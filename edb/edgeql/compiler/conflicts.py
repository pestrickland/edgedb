#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""Compilation of DML exclusive constraint conflict handling."""


from __future__ import annotations
from typing import *

from edb import errors
from edb.common import context as pctx

from edb.ir import ast as irast
from edb.ir import typeutils

from edb.schema import constraints as s_constr
from edb.schema import name as s_name
from edb.schema import objtypes as s_objtypes
from edb.schema import pointers as s_pointers
from edb.schema import utils as s_utils

from edb.edgeql import ast as qlast
from edb.edgeql import utils as qlutils

from . import astutils
from . import context
from . import dispatch
from . import inference
from . import setgen
from . import typegen


def _compile_conflict_select(
    stmt: irast.MutatingStmt,
    subject_typ: s_objtypes.ObjectType,
    *,
    for_inheritance: bool,
    fake_dml_set: Optional[irast.Set],
    obj_constrs: Sequence[s_constr.Constraint],
    constrs: Dict[str, Tuple[s_pointers.Pointer, List[s_constr.Constraint]]],
    parser_context: Optional[pctx.ParserContext],
    ctx: context.ContextLevel,
) -> Optional[qlast.Expr]:
    """Synthesize a select of conflicting objects

    ... for a single object type. This gets called once for each ancestor
    type that provides constraints to the type being inserted.

    `cnstrs` contains the constraints to consider.
    """
    # Find which pointers we need to grab
    needed_ptrs = set(constrs)
    for constr in obj_constrs:
        subjexpr = constr.get_subjectexpr(ctx.env.schema)
        assert subjexpr
        needed_ptrs |= qlutils.find_subject_ptrs(subjexpr.qlast)

    wl = list(needed_ptrs)
    ptr_anchors = {}
    while wl:
        p = wl.pop()
        ptr = subject_typ.getptr(ctx.env.schema, s_name.UnqualName(p))
        if expr := ptr.get_expr(ctx.env.schema):
            assert isinstance(expr.qlast, qlast.Expr)
            ptr_anchors[p] = expr.qlast
            for ref in qlutils.find_subject_ptrs(expr.qlast):
                if ref not in needed_ptrs:
                    wl.append(ref)
                    needed_ptrs.add(ref)

    ctx.anchors = ctx.anchors.copy()

    # If we are given a fake_dml_set to directly represent the result
    # of our DML, use that instead of populating the result.
    if fake_dml_set:
        for p in needed_ptrs | {'id'}:
            ptr = subject_typ.getptr(ctx.env.schema, s_name.UnqualName(p))
            val = setgen.extend_path(fake_dml_set, ptr, ctx=ctx)

            ptr_anchors[p] = ctx.create_anchor(val, p)

    # Find the IR corresponding to the fields we care about and
    # produce anchors for them
    ptrs_in_shape = set()
    for elem, _ in stmt.subject.shape:
        assert elem.rptr is not None
        name = elem.rptr.ptrref.shortname.name
        ptrs_in_shape.add(name)
        if name in needed_ptrs and name not in ptr_anchors:
            assert elem.expr
            if inference.infer_volatility(elem.expr, ctx.env).is_volatile():
                if for_inheritance:
                    error = (
                        'INSERT does not support volatile properties with '
                        'exclusive constraints when another statement in '
                        'the same query modifies a related type'
                    )
                else:
                    error = (
                        'INSERT UNLESS CONFLICT ON does not support volatile '
                        'properties'
                    )
                raise errors.UnsupportedFeatureError(
                    error, context=parser_context
                )

            # FIXME: The wrong thing will definitely happen if there are
            # volatile entries here
            ptr_anchors[name] = ctx.create_anchor(
                setgen.ensure_set(elem.expr, ctx=ctx), name)

    if for_inheritance and not ptrs_in_shape:
        return None

    # Fill in empty sets for pointers that are needed but not present
    present_ptrs = set(ptr_anchors)
    for p in (needed_ptrs - present_ptrs):
        ptr = subject_typ.getptr(ctx.env.schema, s_name.UnqualName(p))
        typ = ptr.get_target(ctx.env.schema)
        assert typ
        ptr_anchors[p] = qlast.TypeCast(
            expr=qlast.Set(elements=[]),
            type=typegen.type_to_ql_typeref(typ, ctx=ctx))

    if not ptr_anchors:
        raise errors.QueryError(
            'INSERT UNLESS CONFLICT property requires matching shape',
            context=parser_context,
        )

    conds: List[qlast.Expr] = []
    for ptrname, (ptr, ptr_cnstrs) in constrs.items():
        if ptrname not in present_ptrs:
            continue
        anchor = qlutils.subject_paths_substitute(
            ptr_anchors[ptrname], ptr_anchors)
        ptr_val = qlast.Path(partial=True, steps=[
            qlast.Ptr(ptr=qlast.ObjectRef(name=ptrname))
        ])
        ptr, ptr_cnstrs = constrs[ptrname]
        ptr_card = ptr.get_cardinality(ctx.env.schema)

        for cnstr in ptr_cnstrs:
            lhs: qlast.Expr = anchor
            rhs: qlast.Expr = ptr_val
            # If there is a subjectexpr, substitute our lhs and rhs in
            # for __subject__ in the subjectexpr and compare *that*
            if (subjectexpr := cnstr.get_subjectexpr(ctx.env.schema)):
                assert isinstance(subjectexpr.qlast, qlast.Expr)
                lhs = qlutils.subject_substitute(subjectexpr.qlast, lhs)
                rhs = qlutils.subject_substitute(subjectexpr.qlast, rhs)

            conds.append(qlast.BinOp(
                op='=' if ptr_card.is_single() else 'IN',
                left=lhs, right=rhs,
            ))

    insert_subject = qlast.Path(steps=[
        s_utils.name_to_ast_ref(subject_typ.get_name(ctx.env.schema))])

    for constr in obj_constrs:
        # TODO: learn to skip irrelevant ones for UPDATEs at least?
        subjectexpr = constr.get_subjectexpr(ctx.env.schema)
        assert subjectexpr and isinstance(subjectexpr.qlast, qlast.Expr)
        lhs = qlutils.subject_paths_substitute(subjectexpr.qlast, ptr_anchors)
        rhs = qlutils.subject_substitute(subjectexpr.qlast, insert_subject)
        conds.append(qlast.BinOp(op='=', left=lhs, right=rhs))

    if not conds:
        return None

    # We use `any` to compute the disjunction here because some might
    # be empty.
    if len(conds) == 1:
        cond = conds[0]
    else:
        cond = qlast.FunctionCall(
            func='any',
            args=[qlast.Set(elements=conds)],
        )

    # For the result filtering we need to *ignore* the same object
    if fake_dml_set:
        anchor = qlutils.subject_paths_substitute(
            ptr_anchors['id'], ptr_anchors)
        ptr_val = qlast.Path(partial=True, steps=[
            qlast.Ptr(ptr=qlast.ObjectRef(name='id'))
        ])
        cond = qlast.BinOp(
            op='AND',
            left=cond,
            right=qlast.BinOp(op='!=', left=anchor, right=ptr_val),
        )

    # Produce a query that finds the conflicting objects
    select_ast = qlast.DetachedExpr(
        expr=qlast.SelectQuery(result=insert_subject, where=cond)
    )

    return select_ast


def _constr_matters(
    constr: s_constr.Constraint, ctx: context.ContextLevel,
) -> bool:
    schema = ctx.env.schema
    return (
        not constr.generic(schema)
        and not constr.get_delegated(schema)
        and (
            constr.get_owned(schema)
            or all(anc.get_delegated(schema) or anc.generic(schema) for anc
                   in constr.get_ancestors(schema).objects(schema))
        )
    )


PointerConstraintMap = Dict[
    str,
    Tuple[s_pointers.Pointer, List[s_constr.Constraint]],
]
ConstraintPair = Tuple[PointerConstraintMap, List[s_constr.Constraint]]
ConflictTypeMap = Dict[s_objtypes.ObjectType, ConstraintPair]


def _split_constraints(
    obj_constrs: Sequence[s_constr.Constraint],
    constrs: PointerConstraintMap,
    ctx: context.ContextLevel,
) -> ConflictTypeMap:
    schema = ctx.env.schema

    type_maps: ConflictTypeMap = {}

    # Split up pointer constraints by what object types they come from
    for name, (_, p_constrs) in constrs.items():
        for p_constr in p_constrs:
            ancs = (p_constr,) + p_constr.get_ancestors(schema).objects(schema)
            for anc in ancs:
                if not _constr_matters(anc, ctx):
                    continue
                p_ptr = anc.get_subject(schema)
                assert isinstance(p_ptr, s_pointers.Pointer)
                obj = p_ptr.get_source(schema)
                assert isinstance(obj, s_objtypes.ObjectType)
                map, _ = type_maps.setdefault(obj, ({}, []))
                _, entry = map.setdefault(name, (p_ptr, []))
                entry.append(anc)

    # Split up object constraints by what object types they come from
    for obj_constr in obj_constrs:
        ancs = (obj_constr,) + obj_constr.get_ancestors(schema).objects(schema)
        for anc in ancs:
            if not _constr_matters(anc, ctx):
                continue
            obj = anc.get_subject(schema)
            assert isinstance(obj, s_objtypes.ObjectType)
            _, o_constr_entry = type_maps.setdefault(obj, ({}, []))
            o_constr_entry.append(anc)

    return type_maps


def compile_conflict_select(
    stmt: irast.MutatingStmt,
    subject_typ: s_objtypes.ObjectType,
    *,
    for_inheritance: bool=False,
    fake_dml_set: Optional[irast.Set]=None,
    obj_constrs: Sequence[s_constr.Constraint],
    constrs: PointerConstraintMap,
    parser_context: Optional[pctx.ParserContext],
    ctx: context.ContextLevel,
) -> Tuple[irast.Set, bool, bool]:
    """Synthesize a select of conflicting objects

    This teases apart the constraints we care about based on which
    type they originate from, generates a SELECT for each type, and
    unions them together.

    `cnstrs` contains the constraints to consider.
    """
    schema = ctx.env.schema

    if for_inheritance:
        type_maps = {subject_typ: (constrs, list(obj_constrs))}
    else:
        type_maps = _split_constraints(obj_constrs, constrs, ctx=ctx)

    # Generate a separate query for each type
    from_parent = False
    frags = []
    for a_obj, (a_constrs, a_obj_constrs) in type_maps.items():
        frag = _compile_conflict_select(
            stmt, a_obj, obj_constrs=a_obj_constrs, constrs=a_constrs,
            for_inheritance=for_inheritance,
            fake_dml_set=fake_dml_set,
            parser_context=parser_context, ctx=ctx,
        )
        if frag:
            if a_obj != subject_typ:
                from_parent = True
            frags.append(frag)

    always_check = from_parent or any(
        not child.is_view(schema) for child in subject_typ.children(schema)
    )

    # Union them all together
    select_ast = qlast.Set(elements=frags)
    with ctx.new() as ectx:
        ectx.implicit_limit = 0
        select_ir = dispatch.compile(select_ast, ctx=ectx)
        select_ir = setgen.scoped_set(
            select_ir, force_reassign=True, ctx=ectx)
        assert isinstance(select_ir, irast.Set)

    return select_ir, always_check, from_parent


def _get_exclusive_ptr_constraints(
    typ: s_objtypes.ObjectType,
    *, ctx: context.ContextLevel,
) -> Dict[str, Tuple[s_pointers.Pointer, List[s_constr.Constraint]]]:
    schema = ctx.env.schema
    pointers = {}

    exclusive_constr = schema.get('std::exclusive', type=s_constr.Constraint)
    for ptr in typ.get_pointers(schema).objects(schema):
        ptr = ptr.get_nearest_non_derived_parent(schema)
        ex_cnstrs = [c for c in ptr.get_constraints(schema).objects(schema)
                     if c.issubclass(schema, exclusive_constr)]
        if ex_cnstrs:
            name = ptr.get_shortname(schema).name
            if name != 'id':
                pointers[name] = ptr, ex_cnstrs

    return pointers


def compile_insert_unless_conflict(
    stmt: irast.InsertStmt,
    typ: s_objtypes.ObjectType,
    *, ctx: context.ContextLevel,
) -> irast.OnConflictClause:
    """Compile an UNLESS CONFLICT clause with no ON

    This requires synthesizing a conditional based on all the exclusive
    constraints on the object.
    """
    pointers = _get_exclusive_ptr_constraints(typ, ctx=ctx)
    obj_constrs = typ.get_constraints(ctx.env.schema).objects(ctx.env.schema)

    select_ir, always_check, _ = compile_conflict_select(
        stmt, typ,
        constrs=pointers,
        obj_constrs=obj_constrs,
        parser_context=stmt.context, ctx=ctx)

    return irast.OnConflictClause(
        constraint=None, select_ir=select_ir, always_check=always_check,
        else_ir=None)


def compile_insert_unless_conflict_on(
    stmt: irast.InsertStmt,
    typ: s_objtypes.ObjectType,
    constraint_spec: qlast.Expr,
    else_branch: Optional[qlast.Expr],
    *, ctx: context.ContextLevel,
) -> irast.OnConflictClause:

    with ctx.new() as constraint_ctx:
        constraint_ctx.partial_path_prefix = stmt.subject

        # We compile the name here so we can analyze it, but we don't do
        # anything else with it.
        cspec_res = dispatch.compile(constraint_spec, ctx=constraint_ctx)

    # We accept a property, link, or a list of them in the form of a
    # tuple.
    if cspec_res.rptr is None and isinstance(cspec_res.expr, irast.Tuple):
        cspec_args = [elem.val for elem in cspec_res.expr.elements]
    else:
        cspec_args = [cspec_res]

    for cspec_arg in cspec_args:
        if not cspec_arg.rptr:
            raise errors.QueryError(
                'UNLESS CONFLICT argument must be a property, link, '
                'or tuple of properties and links',
                context=constraint_spec.context,
            )

        if cspec_arg.rptr.source.path_id != stmt.subject.path_id:
            raise errors.QueryError(
                'UNLESS CONFLICT argument must be a property of the '
                'type being inserted',
                context=constraint_spec.context,
            )

    schema = ctx.env.schema

    ptrs = []
    exclusive_constr = schema.get('std::exclusive', type=s_constr.Constraint)
    for cspec_arg in cspec_args:
        assert cspec_arg.rptr is not None
        schema, ptr = (
            typeutils.ptrcls_from_ptrref(cspec_arg.rptr.ptrref, schema=schema))
        if not isinstance(ptr, s_pointers.Pointer):
            raise errors.QueryError(
                'UNLESS CONFLICT property must be a property',
                context=constraint_spec.context,
            )

        ptr = ptr.get_nearest_non_derived_parent(schema)
        ptr_card = ptr.get_cardinality(schema)
        if not ptr_card.is_single():
            raise errors.QueryError(
                'UNLESS CONFLICT property must be a SINGLE property',
                context=constraint_spec.context,
            )

        ptrs.append(ptr)

    obj_constrs = inference.cardinality.get_object_exclusive_constraints(
        typ, set(ptrs), ctx.env)

    field_constrs = []
    if len(ptrs) == 1:
        field_constrs = [
            c for c in ptrs[0].get_constraints(schema).objects(schema)
            if c.issubclass(schema, exclusive_constr)]

    all_constrs = list(obj_constrs) + field_constrs
    if len(all_constrs) != 1:
        raise errors.QueryError(
            'UNLESS CONFLICT property must have a single exclusive constraint',
            context=constraint_spec.context,
        )

    ds = {ptr.get_shortname(schema).name: (ptr, field_constrs)
          for ptr in ptrs}
    select_ir, always_check, from_anc = compile_conflict_select(
        stmt, typ, constrs=ds, obj_constrs=list(obj_constrs),
        parser_context=stmt.context, ctx=ctx)

    # Compile an else branch
    else_ir = None
    if else_branch:
        # TODO: We should support this, but there is some semantic and
        # implementation trickiness.
        if from_anc:
            raise errors.UnsupportedFeatureError(
                'UNLESS CONFLICT can not use ELSE when constraint is from a '
                'parent type',
                context=constraint_spec.context,
            )

        # The ELSE needs to be able to reference the subject in an
        # UPDATE, even though that would normally be prohibited.
        ctx.path_scope.factoring_allowlist.add(stmt.subject.path_id)

        # Compile else
        else_ir = dispatch.compile(
            astutils.ensure_qlstmt(else_branch), ctx=ctx)
        assert isinstance(else_ir, irast.Set)

    return irast.OnConflictClause(
        constraint=irast.ConstraintRef(id=all_constrs[0].id),
        select_ir=select_ir,
        always_check=always_check,
        else_ir=else_ir
    )


def compile_inheritance_conflict_selects(
    stmt: irast.MutatingStmt,
    conflict: irast.MutatingStmt,
    typ: s_objtypes.ObjectType,
    subject_type: s_objtypes.ObjectType,
    *, ctx: context.ContextLevel,
) -> List[irast.OnConflictClause]:
    """Compile the selects needed to resolve multiple DML to related types

    Generate a SELECT that finds all objects of type `typ` that conflict with
    the insert `stmt`. The backend will use this to explicitly check that
    no conflicts exist, and raise an error if they do.

    This is needed because we mostly use triggers to enforce these
    cross-type exclusive constraints, and they use a snapshot
    beginning at the start of the statement.
    """
    pointers = _get_exclusive_ptr_constraints(typ, ctx=ctx)
    obj_constrs = typ.get_constraints(ctx.env.schema).objects(
        ctx.env.schema)

    # This is a little silly, but for *this* we need to do one per
    # constraint (so that we can properly identify which constraint
    # failed in the error messages)
    entries: List[Tuple[s_constr.Constraint, ConstraintPair]] = []
    for name, (ptr, ptr_constrs) in pointers.items():
        for ptr_constr in ptr_constrs:
            if _constr_matters(ptr_constr, ctx):
                entries.append((ptr_constr, ({name: (ptr, [ptr_constr])}, [])))
    for obj_constr in obj_constrs:
        if _constr_matters(obj_constr, ctx):
            entries.append((obj_constr, ({}, [obj_constr])))

    # For updates, we need to pull from the actual result overlay,
    # since the final row can depend on things not in the query.
    fake_dml_set = None
    if isinstance(stmt, irast.UpdateStmt):
        fake_subject = qlast.DetachedExpr(expr=qlast.Path(steps=[
            s_utils.name_to_ast_ref(subject_type.get_name(ctx.env.schema))]))

        fake_dml_set = dispatch.compile(fake_subject, ctx=ctx)

    clauses = []
    for cnstr, (p, o) in entries:
        select_ir, _, _ = compile_conflict_select(
            stmt, typ,
            for_inheritance=True,
            fake_dml_set=fake_dml_set,
            constrs=p,
            obj_constrs=o,
            parser_context=stmt.context, ctx=ctx)
        if isinstance(select_ir, irast.EmptySet):
            continue
        cnstr_ref = irast.ConstraintRef(id=cnstr.id)
        clauses.append(
            irast.OnConflictClause(
                constraint=cnstr_ref, select_ir=select_ir, always_check=False,
                else_ir=None, else_fail=conflict,
                update_query_set=fake_dml_set)
        )
    return clauses


def compile_inheritance_conflict_checks(
    stmt: irast.MutatingStmt,
    subject_stype: s_objtypes.ObjectType,
    *, ctx: context.ContextLevel,
) -> Optional[List[irast.OnConflictClause]]:
    if not ctx.env.dml_stmts:
        return None

    assert isinstance(subject_stype, s_objtypes.ObjectType)
    # TODO: when the conflicting statement is an UPDATE, only
    # look at things it updated
    modified_ancestors = set()
    base_object = ctx.env.schema.get(
        'std::BaseObject', type=s_objtypes.ObjectType)

    subject_stypes = [subject_stype]
    # For updates, we need to also consider all descendants, because
    # those could also have interesting constraints of their own.
    if isinstance(stmt, irast.UpdateStmt):
        subject_stypes.extend(subject_stype.descendants(ctx.env.schema))

    # N.B that for updates, the update itself will be in dml_stmts,
    # since an update can conflict with itself if there are subtypes.
    for ir in ctx.env.dml_stmts:
        typ = setgen.get_set_type(ir.subject, ctx=ctx)
        assert isinstance(typ, s_objtypes.ObjectType)
        typ = typ.get_nearest_non_derived_parent(ctx.env.schema)

        typs = [typ]
        # As mentioned above, need to consider descendants of updates
        if isinstance(ir, irast.UpdateStmt):
            typs.extend(typ.descendants(ctx.env.schema))

        for typ in typs:
            if typ.is_view(ctx.env.schema):
                continue

            for subject_stype in subject_stypes:
                if subject_stype.is_view(ctx.env.schema):
                    continue

                # If the earlier DML has a shared ancestor that isn't
                # BaseObject and isn't (if it's an insert) the same type,
                # then we need to see if we need a conflict select
                if (
                    subject_stype == typ
                    and not isinstance(ir, irast.UpdateStmt)
                    and not isinstance(stmt, irast.UpdateStmt)
                ):
                    continue
                ancs = s_utils.get_class_nearest_common_ancestors(
                    ctx.env.schema, [subject_stype, typ])
                for anc in ancs:
                    if anc != base_object:
                        modified_ancestors.add((subject_stype, anc, ir))

    conflicters = []
    for subject_stype, anc_type, ir in modified_ancestors:
        conflicters.extend(compile_inheritance_conflict_selects(
            stmt, ir, anc_type, subject_stype, ctx=ctx))

    return conflicters or None
