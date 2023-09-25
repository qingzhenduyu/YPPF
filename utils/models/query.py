'''查询引用字段

提供获取查询字段的函数，且同时支持字段描述符、字段实例以及字段名称。

在以往的代码中，我们经常会看到这样的代码::

    class AModel(Model):
        fkey_name = models.ForeignKey(...)

    class BModel(Model):
        fkey2_name = models.ForeignKey(A_Model)

当跨越关系查询时，我们需要写很长的代码，这样不仅不美观，而且不利于代码的追踪修改，例如::

    instance = BModel.objects.filter(fkey2_name__fkey_name__field_name=...)
    values = BModel.objects.values('fkey2_name__fkey_name__field_name')

如需修改`fkey2_name`字段的名称，则无法追踪以上代码，因此修改起来非常困难，且容易出错。
这是因为我们使用字符串来引用字段来引用字段。如果我们通过字段本身来引用，就可以避免这个问题。

Warning:
    本模块的函数在模型定义时无法使用，因为字段描述符在模型类创建后才会被创建，但你可以在任何方法中使用。
'''
from typing import cast, Any, TypeAlias, TypeGuard

from django.db.models import Field, Q
from django.db.models.query_utils import DeferredAttribute
from django.db.models.fields.related import RelatedField, ForeignObjectRel
from django.db.models.fields.related_descriptors import (
    ForwardManyToOneDescriptor,
    ForwardOneToOneDescriptor,
    ManyToManyDescriptor,
    ForeignKeyDeferredAttribute,
)
from django.db.models.constants import LOOKUP_SEP


__all__ = ['f', 'q', 'lq']


NormalFieldDescriptor: TypeAlias = DeferredAttribute
NormalFieldLike: TypeAlias = Field | NormalFieldDescriptor
ForwardDescriptor: TypeAlias = ForwardManyToOneDescriptor | ForwardOneToOneDescriptor
ForeignIndexDescriptor: TypeAlias = ForeignKeyDeferredAttribute
RelatedDescriptor: TypeAlias = (ForwardDescriptor
                                | ForeignIndexDescriptor | ManyToManyDescriptor)
RelatedFieldLike: TypeAlias = RelatedField | RelatedDescriptor
FieldLike: TypeAlias = NormalFieldLike | RelatedFieldLike
FieldLikeExpr: TypeAlias = FieldLike | str


def _is_relation(field: FieldLike) -> TypeGuard[RelatedFieldLike]:
    '''判断字段是否为关系字段相关属性'''
    if isinstance(field, Field):
        return field.is_relation
    if isinstance(field, RelatedDescriptor):
        return True
    return False


def _is_foreign_index(field: RelatedFieldLike) -> TypeGuard[ForeignIndexDescriptor]:
    '''判断字段是否为外键索引字段'''
    return isinstance(field, ForeignIndexDescriptor)


def _is_forward_relation(field: RelatedFieldLike) -> bool:
    '''判断字段是否为正向关系字段'''
    if isinstance(field, ManyToManyDescriptor):
        return True
    return isinstance(field, ForwardDescriptor | RelatedField)


def _get_related_field(field: RelatedFieldLike) -> RelatedField:
    '''获取关系字段'''
    if isinstance(field, RelatedField):
        return field
    if isinstance(field, ForeignIndexDescriptor):
        return cast(RelatedField, field.field)
    if isinstance(field, ForwardDescriptor):
        return cast(RelatedField, field.field)
    if isinstance(field, ManyToManyDescriptor):
        return field.field
    assert False, f'{type(field)} is not a related field'


def _normal_name(field: NormalFieldLike) -> str:
    '''获取普通字段的查询名称'''
    # 普通字段的`name`属性代表字段在查询时使用的名称
    # 见`Field.get_filter_kwargs_for_object`
    if isinstance(field, NormalFieldDescriptor):
        field = field.field
    return field.name


def _foreign_index_name(field: RelatedFieldLike) -> str:
    '''获取外键索引字段的查询名称'''
    # `attname`属性代表`ForeignKey`对应数据库字段的名称，即`%field_name%_id`
    return _get_related_field(field).attname


def _forward_name(field: RelatedFieldLike) -> str:
    '''获取正向关系字段的查询名称'''
    # 关联字段的`name`属性代表模型字段的名称
    return _get_related_field(field).name


def _to_field_name(field: FieldLikeExpr) -> str:
    '''获取字段的查询名称'''
    if isinstance(field, str):
        return field
    if _is_relation(field):
        if _is_foreign_index(field):
            return _foreign_index_name(field)
        if _is_forward_relation(field):
            return _forward_name(field)
    elif isinstance(field, NormalFieldLike):
        return _normal_name(field)
    raise TypeError(f'Unsupported type: {type(field)} for field')


def f(*fields: FieldLikeExpr) -> str:
    '''获取连续字段的查询名称'''
    return LOOKUP_SEP.join(_to_field_name(field) for field in fields)


def q(*fields: FieldLikeExpr, value: Any) -> Q:
    '''获取连续字段的查询Q对象'''
    return Q(**{f(*fields): value})


def lq(value: Any, *fields: FieldLikeExpr) -> Q:
    '''获取连续字段的查询Q对象，参数线性排列'''
    return q(*fields, value=value)