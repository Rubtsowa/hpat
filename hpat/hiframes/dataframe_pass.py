import operator
from collections import defaultdict, namedtuple
import numpy as np
import pandas as pd
import warnings

import numba
from numba import ir, ir_utils, types
from numba.ir_utils import (replace_arg_nodes, compile_to_numba_ir,
                            find_topo_order, gen_np_call, get_definition, guard,
                            find_callname, mk_alloc, find_const, is_setitem,
                            is_getitem, mk_unique_var, dprint_func_ir,
                            build_definitions, find_build_sequence)
from numba.inline_closurecall import inline_closure_call
from numba.typing.templates import Signature, bound_function, signature
from numba.typing.arraydecl import ArrayAttribute
from numba.extending import overload
from numba.typing.templates import infer_global, AbstractTemplate, signature
import hpat
from hpat import hiframes
from hpat.utils import (debug_prints, inline_new_blocks, ReplaceFunc,
    is_whole_slice, is_array)
from hpat.str_ext import string_type, unicode_to_std_str, std_str_to_unicode
from hpat.str_arr_ext import (string_array_type, StringArrayType,
    is_str_arr_typ, pre_alloc_string_array)
from hpat.pio_api import h5dataset_type
from hpat.hiframes.rolling import get_rolling_setup_args
from hpat.hiframes.pd_dataframe_ext import (DataFrameType, DataFrameLocType,
    DataFrameILocType, DataFrameIatType)
from hpat.hiframes.pd_series_ext import SeriesType



class DataFramePass(object):
    """Analyze and transform dataframe calls after typing"""

    def __init__(self, func_ir, typingctx, typemap, calltypes):
        self.func_ir = func_ir
        self.typingctx = typingctx
        self.typemap = typemap
        self.calltypes = calltypes

    def run(self):
        blocks = self.func_ir.blocks
        # topo_order necessary so DataFrame data replacement optimization can
        # be performed in one pass
        topo_order = find_topo_order(blocks)
        work_list = list((l, blocks[l]) for l in reversed(topo_order))
        while work_list:
            label, block = work_list.pop()
            new_body = []
            replaced = False
            for i, inst in enumerate(block.body):
                out_nodes = [inst]

                if isinstance(inst, ir.Assign):
                    self.func_ir._definitions[inst.target.name].remove(inst.value)
                    out_nodes = self._run_assign(inst)
                elif isinstance(inst, (ir.SetItem, ir.StaticSetItem)):
                    out_nodes = self._run_setitem(inst)

                if isinstance(out_nodes, list):
                    new_body.extend(out_nodes)
                    self._update_definitions(out_nodes)
                if isinstance(out_nodes, ReplaceFunc):
                    rp_func = out_nodes
                    if rp_func.pre_nodes is not None:
                        new_body.extend(rp_func.pre_nodes)
                        self._update_definitions(rp_func.pre_nodes)
                    # replace inst.value to a call with target args
                    # as expected by inline_closure_call
                    inst.value = ir.Expr.call(None, rp_func.args, (), inst.loc)
                    block.body = new_body + block.body[i:]
                    inline_closure_call(self.func_ir, rp_func.glbls,
                        block, len(new_body), rp_func.func, self.typingctx,
                        rp_func.arg_types,
                        self.typemap, self.calltypes, work_list)
                    replaced = True
                    break
                if isinstance(out_nodes, dict):
                    block.body = new_body + block.body[i:]
                    inline_new_blocks(self.func_ir, block, i, out_nodes, work_list)
                    replaced = True
                    break

            if not replaced:
                blocks[label].body = new_body

        while ir_utils.remove_dead(self.func_ir.blocks, self.func_ir.arg_names,
                                   self.func_ir, self.typemap):
            pass

        self.func_ir._definitions = build_definitions(self.func_ir.blocks)
        dprint_func_ir(self.func_ir, "after hiframes_typed")
        return

    def _run_assign(self, assign):
        lhs = assign.target.name
        rhs = assign.value


        if isinstance(rhs, ir.Expr):
            if rhs.op == 'getattr':
                return self._run_getattr(assign, rhs)

            # if rhs.op == 'binop':
            #     return self._run_binop(assign, rhs)

            # # XXX handling inplace_binop similar to binop for now
            # # TODO handle inplace alignment
            # if rhs.op == 'inplace_binop':
            #     return self._run_binop(assign, rhs)

            # if rhs.op == 'unary':
            #     return self._run_unary(assign, rhs)

            # replace getitems on dataframe
            if rhs.op in ('getitem', 'static_getitem'):
                return self._run_getitem(assign, rhs)

            if rhs.op == 'call':
                return self._run_call(assign, lhs, rhs)

        return [assign]

    def _run_getitem(self, assign, rhs):
        lhs = assign.target
        nodes = []
        index_var = (rhs.index_var if rhs.op == 'static_getitem'
                                    else rhs.index)
        index_typ = self.typemap[index_var.name]

        # A = df['column'] or df[['C1', 'C2']]
        if rhs.op == 'static_getitem' and self._is_df_var(rhs.value):
            df_var = rhs.value
            df_typ = self.typemap[df_var.name]
            index = rhs.index

            # A = df['column']
            if isinstance(index, str):
                if index not in df_typ.columns:
                    raise ValueError(
                        "dataframe {} does not include column {}".format(
                            df_var.name, index))

                arr = self._get_dataframe_data(df_var, index, nodes)
                # TODO: index
                return  self._replace_func(
                    lambda A: hpat.hiframes.api.init_series(A),
                    [arr], pre_nodes=nodes)

            # df[['C1', 'C2']]
            if isinstance(index, list) and all(isinstance(c, str)
                                                               for c in index):
                nodes = []
                in_arrs = [self._get_dataframe_data(df_var, c, nodes)
                            for c in index]
                out_arrs = [self._gen_arr_copy(A, nodes) for A in in_arrs]
                #
                n_cols = len(index)
                data_args = ", ".join('data{}'.format(i) for i in range(n_cols))
                col_args = ", ".join("'{}'".format(c) for c in index)
                func_text = "def _init_df({}):\n".format(data_args)
                func_text += "  return hpat.hiframes.pd_dataframe_ext.init_dataframe({}, None, {})\n".format(
                    data_args, col_args)
                loc_vars = {}
                exec(func_text, {}, loc_vars)
                _init_df = loc_vars['_init_df']
                return self._replace_func(_init_df, out_arrs, pre_nodes=nodes)
                # raise ValueError("unsupported dataframe access {}[{}]".format(
                #                  rhs.value.name, index))

        # df1 = df[df.A > .5]
        if self.is_bool_arr(index_var.name) and self._is_df_var(rhs.value):
            df_var = rhs.value
            return self._gen_df_filter(df_var, index_var, lhs)

        # df.loc[df.A > .5], df.iloc[df.A > .5]
        # df.iloc[1:n], df.iloc[np.array([1,2,3])], ...
        if ((self._is_df_loc_var(rhs.value) or self._is_df_iloc_var(rhs.value))
                and (self.is_bool_arr(index_var.name)
                or self.is_int_list_or_arr(index_var.name)
                or isinstance(index_typ, types.SliceType))):
            # TODO: check for errors
            df_var = guard(get_definition, self.func_ir, rhs.value).value
            return self._gen_df_filter(df_var, index_var, lhs)

        # df.iloc[1:n,0], df.loc[1:n,'A']
        if ((self._is_df_loc_var(rhs.value) or self._is_df_iloc_var(rhs.value))
                and isinstance(index_typ, types.Tuple)
                and len(index_typ) == 2):
            #
            df_var = guard(get_definition, self.func_ir, rhs.value).value
            df_typ = self.typemap[df_var.name]
            ind_def = guard(get_definition, self.func_ir, index_var)
            # TODO check and report errors
            assert isinstance(ind_def, ir.Expr) and ind_def.op == 'build_tuple'

            if self._is_df_iloc_var(rhs.value):
                col_ind = guard(find_const, self.func_ir, ind_def.items[1])
                col_name = df_typ.columns[col_ind]
            else:  # df.loc
                col_name = guard(find_const, self.func_ir, ind_def.items[1])

            col_filter_var = ind_def.items[0]
            name_var = ir.Var(lhs.scope, mk_unique_var('df_col_name'), lhs.loc)
            self.typemap[name_var.name] = types.StringLiteral(col_name)
            nodes.append(
                ir.Assign(ir.Const(col_name, lhs.loc), name_var, lhs.loc))
            in_arr = self._get_dataframe_data(df_var, col_name, nodes)

            if guard(is_whole_slice, self.typemap, self.func_ir, col_filter_var):
                func = lambda A, ind, name: hpat.hiframes.api.init_series(
                    A, None, name)
            else:
                # TODO: test this case
                func = lambda A, ind, name: hpat.hiframes.api.init_series(
                    A[ind], None, name)

            return self._replace_func(func,
                [in_arr, col_filter_var, name_var], pre_nodes=nodes)

        if self._is_df_iat_var(rhs.value):
            df_var = guard(get_definition, self.func_ir, rhs.value).value
            df_typ = self.typemap[df_var.name]
            # df.iat[3,1]
            if (rhs.op == 'static_getitem' and isinstance(rhs.index, tuple)
                    and len(rhs.index) == 2 and isinstance(rhs.index[0], int)
                    and isinstance(rhs.index[1], int)):
                col_ind = rhs.index[1]
                row_ind = rhs.index[0]
                col_name = df_typ.columns[col_ind]
                in_arr = self._get_dataframe_data(df_var, col_name, nodes)
                return self._replace_func(lambda A: A[row_ind], [in_arr],
                    extra_globals={'row_ind': row_ind}, pre_nodes=nodes)

            # df.iat[n,1]
            if isinstance(index_typ, types.Tuple) and len(index_typ) == 2:
                ind_def = guard(get_definition, self.func_ir, index_var)
                col_ind = guard(find_const, self.func_ir, ind_def.items[1])
                col_name = df_typ.columns[col_ind]
                in_arr = self._get_dataframe_data(df_var, col_name, nodes)
                row_ind = ind_def.items[0]
                return self._replace_func(lambda A, row_ind: A[row_ind],
                    [in_arr, row_ind], pre_nodes=nodes)

        nodes.append(assign)
        return nodes

    def _gen_df_filter(self, df_var, index_var, lhs):
        nodes = []
        df_typ = self.typemap[df_var.name]
        in_vars = {}
        out_vars = {}
        for col in df_typ.columns:
            in_arr = self._get_dataframe_data(df_var, col, nodes)
            out_arr = ir.Var(lhs.scope, mk_unique_var(col), lhs.loc)
            self.typemap[out_arr.name] = self.typemap[in_arr.name]
            in_vars[col] = in_arr
            out_vars[col] = out_arr

        nodes.append(hiframes.filter.Filter(
            lhs.name, df_var.name, index_var, out_vars, in_vars, lhs.loc))

        n_cols = len(df_typ.columns)
        data_args = ", ".join('data{}'.format(i) for i in range(n_cols))
        col_args = ", ".join("'{}'".format(c) for c in df_typ.columns)
        func_text = "def _init_df({}):\n".format(data_args)
        func_text += "  return hpat.hiframes.pd_dataframe_ext.init_dataframe({}, None, {})\n".format(
            data_args, col_args)
        loc_vars = {}
        exec(func_text, {}, loc_vars)
        _init_df = loc_vars['_init_df']
        return self._replace_func(
            _init_df, list(out_vars.values()), pre_nodes=nodes)

    def _run_setitem(self, inst):
        target_typ = self.typemap[inst.target.name]
        nodes = []
        index_var = (inst.index_var if isinstance(inst, ir.StaticSetItem)
                                    else inst.index)
        index_typ = self.typemap[index_var.name]

        if self._is_df_iat_var(inst.target):
            df_var = guard(get_definition, self.func_ir, inst.target).value
            df_typ = self.typemap[df_var.name]
            val = inst.value
            # df.iat[n,1] = 3
            if isinstance(index_typ, types.Tuple) and len(index_typ) == 2:
                ind_def = guard(get_definition, self.func_ir, index_var)
                col_ind = guard(find_const, self.func_ir, ind_def.items[1])
                col_name = df_typ.columns[col_ind]
                in_arr = self._get_dataframe_data(df_var, col_name, nodes)
                row_ind = ind_def.items[0]
                def _impl(A, row_ind, val):
                    A[row_ind] = val
                return self._replace_func(_impl,
                    [in_arr, row_ind, val], pre_nodes=nodes)
        return [inst]

    def _run_getattr(self, assign, rhs):
        rhs_type = self.typemap[rhs.value.name]  # get type of rhs value "df"

        # S = df.A (get dataframe column)
        # TODO: check invalid df.Attr?
        if isinstance(rhs_type, DataFrameType) and rhs.attr in rhs_type.columns:
            nodes = []
            col_name = rhs.attr
            arr = self._get_dataframe_data(rhs.value, col_name, nodes)
            index = self._get_dataframe_index(rhs.value, nodes)
            name = ir.Var(arr.scope, mk_unique_var('df_col_name'), arr.loc)
            self.typemap[name.name] = types.StringLiteral(col_name)
            nodes.append(ir.Assign(ir.Const(col_name, arr.loc), name, arr.loc))
            return self._replace_func(
                lambda arr, index, name: hpat.hiframes.api.init_series(
                    arr, index, name), [arr, index, name], pre_nodes=nodes)

        return [assign]

    def _run_binop(self, assign, rhs):

        arg1, arg2 = rhs.lhs, rhs.rhs
        typ1, typ2 = self.typemap[arg1.name], self.typemap[arg2.name]
        if not (isinstance(typ1, DataFrameType) or isinstance(typ2, DataFrameType)):
            return [assign]

        # nodes = []
        # # TODO: support alignment, dt, etc.
        # # S3 = S1 + S2 ->
        # # S3_data = S1_data + S2_data; S3 = init_series(S3_data)
        # if isinstance(typ1, SeriesType):
        #     arg1 = self._get_series_data(arg1, nodes)
        # if isinstance(typ2, SeriesType):
        #     arg2 = self._get_series_data(arg2, nodes)

        # rhs.lhs, rhs.rhs = arg1, arg2
        # self._convert_series_calltype(rhs)

        # # output stays as Array in A += B where A is Array
        # if isinstance(self.typemap[assign.target.name], types.Array):
        #     assert isinstance(self.calltypes[rhs].return_type, types.Array)
        #     nodes.append(assign)
        #     return nodes

        # out_data = ir.Var(
        #     arg1.scope, mk_unique_var(assign.target.name+'_data'), rhs.loc)
        # self.typemap[out_data.name] = self.calltypes[rhs].return_type
        # nodes.append(ir.Assign(rhs, out_data, rhs.loc))
        # return self._replace_func(
        #     lambda data: hpat.hiframes.api.init_series(data, None, None),
        #     [out_data],
        #     pre_nodes=nodes
        # )

    def _run_unary(self, assign, rhs):
        arg = rhs.value
        typ = self.typemap[arg.name]

        if isinstance(typ, DataFrameType):
            nodes = []
            # arg = self._get_series_data(arg, nodes)
            # rhs.value = arg
            # self._convert_series_calltype(rhs)
            # out_data = ir.Var(
            #     arg.scope, mk_unique_var(assign.target.name+'_data'), rhs.loc)
            # self.typemap[out_data.name] = self.calltypes[rhs].return_type
            # nodes.append(ir.Assign(rhs, out_data, rhs.loc))
            # return self._replace_func(
            #     lambda data: hpat.hiframes.api.init_series(data),
            #     [out_data],
            #     pre_nodes=nodes
            # )

        return [assign]

    def _run_call(self, assign, lhs, rhs):
        fdef = guard(find_callname, self.func_ir, rhs, self.typemap)
        if fdef is None:
            from numba.stencil import StencilFunc
            # could be make_function from list comprehension which is ok
            func_def = guard(get_definition, self.func_ir, rhs.func)
            if isinstance(func_def, ir.Expr) and func_def.op == 'make_function':
                return [assign]
            if isinstance(func_def, ir.Global) and isinstance(func_def.value, StencilFunc):
                return [assign]
            warnings.warn(
                "function call couldn't be found for dataframe analysis")
            return [assign]
        else:
            func_name, func_mod = fdef

        if fdef == ('len', 'builtins') and self._is_df_var(rhs.args[0]):
            return self._run_call_len(lhs, rhs.args[0])

        return [assign]

    def _run_call_dataframe(self, assign, lhs, rhs, series_var, func_name):
        pass

    def _run_call_len(self, lhs, df_var):
        df_typ = self.typemap[df_var.name]

        # empty dataframe has 0 len
        if len(df_typ.columns) == 0:
            return [ir.Assign(ir.Const(0, lhs.loc), lhs, lhs.loc)]

        # run len on one of the columns
        # FIXME: it could potentially avoid remove dead for the column if
        # array analysis doesn't replace len() with it's size
        nodes = []
        arr = self._get_dataframe_data(df_var, df_typ.columns[0], nodes)
        def f(df_arr):  # pragma: no cover
            return len(df_arr)
        return self._replace_func(f, [arr], pre_nodes=nodes)

    def _get_const_tup(self, tup_var):
        tup_def = guard(get_definition, self.func_ir, tup_var)
        if isinstance(tup_def, ir.Expr):
            if tup_def.op == 'binop' and tup_def.fn in ('+', operator.add):
                return (self._get_const_tup(tup_def.lhs)
                        + self._get_const_tup(tup_def.rhs))
            if tup_def.op in ('build_tuple', 'build_list'):
                return tup_def.items
        raise ValueError("constant tuple expected")

    def _get_dataframe_data(self, df_var, col_name, nodes):
        # optimization: return data var directly if not ambiguous
        # (no multiple init_dataframe calls for the same df_var with control
        # flow)
        # e.g. A = init_dataframe(A, None, 'A')
        # XXX assuming init_dataframe is the only call to create a dataframe
        # and dataframe._data is never overwritten
        df_typ = self.typemap[df_var.name]
        ind = df_typ.columns.index(col_name)
        var_def = guard(get_definition, self.func_ir, df_var)
        call_def = guard(find_callname, self.func_ir, var_def)
        if call_def == ('init_dataframe', 'hpat.hiframes.pd_dataframe_ext'):
            return var_def.args[ind]

        loc = df_var.loc
        ind_var = ir.Var(df_var.scope, mk_unique_var('col_ind'), loc)
        self.typemap[ind_var.name] = types.IntegerLiteral(ind)
        nodes.append(ir.Assign(ir.Const(ind, loc), ind_var, loc))
        # XXX use get_series_data() for getting data instead of S._data
        # to enable alias analysis
        f_block = compile_to_numba_ir(
            lambda df, c_ind: hpat.hiframes.pd_dataframe_ext.get_dataframe_data(
                df, c_ind),
            {'hpat': hpat},
            self.typingctx,
            (df_typ, self.typemap[ind_var.name]),
            self.typemap,
            self.calltypes
        ).blocks.popitem()[1]
        replace_arg_nodes(f_block, [df_var, ind_var])
        nodes += f_block.body[:-2]
        return nodes[-1].target

    def _get_dataframe_index(self, df_var, nodes):
        df_typ = self.typemap[df_var.name]
        n_cols = len(df_typ.columns)
        var_def = guard(get_definition, self.func_ir, df_var)
        call_def = guard(find_callname, self.func_ir, var_def)
        if call_def == ('init_dataframe', 'hpat.hiframes.pd_dataframe_ext'):
            return var_def.args[n_cols]

        # XXX use get_series_data() for getting data instead of S._data
        # to enable alias analysis
        f_block = compile_to_numba_ir(
            lambda df: hpat.hiframes.pd_dataframe_ext.get_dataframe_index(df),
            {'hpat': hpat},
            self.typingctx,
            (df_typ,),
            self.typemap,
            self.calltypes
        ).blocks.popitem()[1]
        replace_arg_nodes(f_block, [df_var])
        nodes += f_block.body[:-2]
        return nodes[-1].target

    def _replace_func(self, func, args, const=False,
                      pre_nodes=None, extra_globals=None, pysig=None, kws=None):
        glbls = {'numba': numba, 'np': np, 'hpat': hpat}
        if extra_globals is not None:
            glbls.update(extra_globals)

        # create explicit arg variables for defaults if func has any
        # XXX: inine_closure_call() can't handle defaults properly
        if pysig is not None:
            pre_nodes = [] if pre_nodes is None else pre_nodes
            scope = next(iter(self.func_ir.blocks.values())).scope
            loc = scope.loc
            def normal_handler(index, param, default):
                return default

            def default_handler(index, param, default):
                d_var = ir.Var(scope, mk_unique_var('defaults'), loc)
                self.typemap[d_var.name] = numba.typeof(default)
                node = ir.Assign(ir.Const(default, loc), d_var, loc)
                pre_nodes.append(node)
                return d_var

            # TODO: stararg needs special handling?
            args = numba.typing.fold_arguments(
                pysig, args, kws, normal_handler, default_handler,
                normal_handler)

        arg_typs = tuple(self.typemap[v.name] for v in args)

        if const:
            new_args = []
            for i, arg in enumerate(args):
                val = guard(find_const, self.func_ir, arg)
                if val:
                    new_args.append(types.literal(val))
                else:
                    new_args.append(arg_typs[i])
            arg_typs = tuple(new_args)
        return ReplaceFunc(func, arg_typs, args, glbls, pre_nodes)

    def _is_df_var(self, var):
        return isinstance(self.typemap[var.name], DataFrameType)

    def _is_df_loc_var(self, var):
        return isinstance(self.typemap[var.name], DataFrameLocType)

    def _is_df_iloc_var(self, var):
        return isinstance(self.typemap[var.name], DataFrameILocType)

    def _is_df_iat_var(self, var):
        return isinstance(self.typemap[var.name], DataFrameIatType)

    def is_bool_arr(self, varname):
        typ = self.typemap[varname]
        return (isinstance(typ, (SeriesType, types.Array))
            and typ.dtype == types.bool_)

    def is_int_list_or_arr(self, varname):
        typ = self.typemap[varname]
        return (isinstance(typ, (SeriesType, types.Array, types.List))
            and isinstance(typ.dtype, types.Integer))

    def _is_const_none(self, var):
        var_def = guard(get_definition, self.func_ir, var)
        return isinstance(var_def, ir.Const) and var_def.value is None

    def _update_definitions(self, node_list):
        dumm_block = ir.Block(None, None)
        dumm_block.body = node_list
        build_definitions({0: dumm_block}, self.func_ir._definitions)
        return

    def _get_arg(self, f_name, args, kws, arg_no, arg_name, default=None,
                                                                 err_msg=None):
        arg = None
        if len(args) > arg_no:
            arg = args[arg_no]
        elif arg_name in kws:
            arg = kws[arg_name]

        if arg is None:
            if default is not None:
                return default
            if err_msg is None:
                err_msg = "{} requires '{}' argument".format(f_name, arg_name)
            raise ValueError(err_msg)
        return arg

    def _gen_arr_copy(self, in_arr, nodes):
        f_block = compile_to_numba_ir(
            lambda A: A.copy(), {}, self.typingctx,
            (self.typemap[in_arr.name],), self.typemap, self.calltypes
            ).blocks.popitem()[1]
        replace_arg_nodes(f_block, [in_arr])
        nodes += f_block.body[:-2]
        return nodes[-1].target