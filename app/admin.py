import openpyxl
import random
from datetime import datetime
import csv
import io

from django.http import HttpResponse
from django.urls import path, reverse
from django.shortcuts import render, redirect
from django.contrib import admin, messages
from django.db.models import QuerySet
from django.db import transaction
from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils.safestring import mark_safe

from utils.http.dependency import HttpRequest
from utils.models.query import sfilter, f
from utils.admin_utils import *
from app.models import *
from app.config import GLOBAL_CONFIG
from scheduler.cancel import remove_job
from app.org_utils import accept_modifyorg_submit

from Appointment.models import Participant

class ExcelImportForm(forms.Form):
    """
    用于上传Excel的Form，只包含一个上传文件的字段
    """
    excel_file = forms.FileField(
        label=_("请选择要上传的Excel文件"),
        required=True
    )
# 通用内联模型
@readonly_inline
class PositionInline(admin.TabularInline):
    model = Position
    classes = ['collapse']
    ordering = ['-id']
    fields = [
        'person', 'org',
        'year', 'semester',
        'is_admin', 'pos', 'status',
    ]
    show_change_link = True


# 后台模型
@admin.register(NaturalPerson)
class NaturalPersonAdmin(admin.ModelAdmin):
    _m = NaturalPerson
    list_display = [
        f(_m.person_id),
        f(_m.name),
        f(_m.identity),
    ]
    search_fields = [f(_m.person_id, User.username), f(_m.name)]
    readonly_fields = [f(_m.stu_id_dbonly)]
    list_filter = [
        f(_m.status), f(_m.identity),
        f(_m.wechat_receive_level),
        f(_m.stu_grade), f(_m.stu_class),
    ]

    inlines = [PositionInline]

    def _show_by_option(self, obj: NaturalPerson | None, option: str, detail: str):
        if obj is None or getattr(obj, option):
            return option, detail
        return option

    def get_normal_fields(self, request, obj: NaturalPerson = None):
        _m = NaturalPerson
        fields = []
        fields.append((f(_m.person_id), f(_m.stu_id_dbonly)))
        fields.append(f(_m.name))
        fields.append(self._show_by_option(obj, f(_m.show_nickname), f(_m.nickname)))
        fields.append(self._show_by_option(obj, f(_m.show_gender), f(_m.gender)))
        fields.extend([
            f(_m.identity), f(_m.status),
            f(_m.wechat_receive_level),
            f(_m.accept_promote), f(_m.active_score),
        ])
        return fields

    def get_student_fields(self, request, obj: NaturalPerson = None):
        _m = NaturalPerson
        fields = []
        fields.append(f(_m.stu_grade))
        fields.append(f(_m.stu_class))
        fields.append(self._show_by_option(obj, f(_m.show_major), f(_m.stu_major)))
        fields.append(self._show_by_option(obj, f(_m.show_email), f(_m.email)))
        fields.append(self._show_by_option(obj, f(_m.show_tel), f(_m.telephone)))
        fields.append(self._show_by_option(obj, f(_m.show_dorm), f(_m.stu_dorm)))
        fields.append(self._show_by_option(obj, f(_m.show_birthday), f(_m.birthday)))
        return fields

    # 无论如何都不显示的字段
    exclude = [
        f(_m.avatar), f(_m.wallpaper), f(_m.QRcode), f(_m.biography),
        f(_m.unsubscribe_list),
    ]

    def get_fieldsets(self, request, obj=None):
        fieldsets = [
            (None, {'fields': self.get_normal_fields(request, obj)}),
            ('学生信息', {'classes': ('collapse',), 
                      'fields': self.get_student_fields(request, obj)}),
        ]
        return fieldsets

    def view_on_site(self, obj: NaturalPerson):
        return obj.get_absolute_url()

    actions = [
        'set_student', 'set_teacher',
        'set_graduate', 'set_ungraduate',
        'all_subscribe', 'all_unsubscribe',
        ]

    @as_action("设为 学生", update=True)
    def set_student(self, request, queryset):
        queryset.update(identity=NaturalPerson.Identity.STUDENT)
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("设为 老师", update=True)
    def set_teacher(self, request, queryset):
        queryset.update(identity=NaturalPerson.Identity.TEACHER)
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("设为 已毕业", update=True)
    def set_graduate(self, request, queryset):
        queryset.update(status=NaturalPerson.GraduateStatus.GRADUATED)
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("设为 未毕业", update=True)
    def set_ungraduate(self, request, queryset):
        queryset.update(status=NaturalPerson.GraduateStatus.UNDERGRADUATED)
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("设置 全部订阅")
    def all_subscribe(self, request, queryset):
        for org in queryset:
            org.unsubscribers.clear()
            org.save()
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("设置 取消订阅")
    def all_unsubscribe(self, request, queryset):
        orgs = list(Organization.objects.exclude(
            otype__otype_id=0).values_list('id', flat=True))
        for person in queryset:
            person.unsubscribers.set(orgs)
            person.save()
        return self.message_user(request=request,
                                 message='修改成功!已经取消所有非官方组织的订阅!')

    # 2024.12.28 以下为批量导入逻辑

    change_list_template = "admin/naturalperson_changelist.html"

    def get_urls(self):
        """
        重写get_urls，为Admin添加一个新的url映射(import_excel)。
        """
        urls = super().get_urls()
        my_urls = [
            path("import-excel/", self.admin_site.admin_view(self.import_excel_view),
                 name="naturalperson_import_excel"),
        ]
        return my_urls + urls

    def import_excel_view(self, request):
        """
        用于上传并处理Excel文件的自定义视图。
        """
        # 获取Admin的上下文
        context = dict(
            self.admin_site.each_context(request),
            opts=self.model._meta,  # 用于模板中显示标题等信息
        )

        if request.method == "POST":
            form = ExcelImportForm(request.POST, request.FILES)
            if form.is_valid():
                excel_file = request.FILES["excel_file"]
                password_list = []  # List to store (username, password) tuples
                try:
                    with transaction.atomic():
                        # 使用 openpyxl 读取 Excel
                        wb = openpyxl.load_workbook(excel_file)
                        sheet = wb.active  # 默认读取第一个sheet

                        # 从表格的第二行开始读(假设第一行为标题)
                        for row_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):

                            if row_idx == 1:
                                # 第一行标题跳过
                                continue

                            # row是一个元组
                            sid = str(row[0]).strip()
                            name_value = str(row[1]).strip()
                            gender = str(row[2]).strip()
                            email = str(row[3]).strip()
                            student_or_teacher = str(row[4]).strip()
                            # ... 视情况增加更多字段

                            # 如果有字段都是空的，跳过
                            empty_symbol = [None, "", "None"]
                            if any(field in empty_symbol for field in (sid, name_value, gender, email, student_or_teacher)):
                                continue

                            # 数据验证
                            assert '@' in email, f"第{row_idx}行的邮箱格式:{email}不正确"
                            assert gender in (
                                "男", "女"), f"第{row_idx}行的性别:{gender}格式不正确"
                            assert student_or_teacher in (
                                "学生", "教师"), f"第{row_idx}行的身份格式P{student_or_teacher}不正确"

                            # 生成性别枚举
                            np_gender = (
                                NaturalPerson.Gender.MALE if gender == "男" else NaturalPerson.Gender.FEMALE
                            )

                            # 生成密码，这里假设密码为SID，您可以根据需求更改
                            password = GLOBAL_CONFIG.hasher.encode(
                                sid)[:12]  # 或者使用其他生成方式

                            # 创建用户
                            user = User.objects.create_user(
                                username=sid,
                                name=name_value,
                                usertype=User.Type.STUDENT if student_or_teacher == "学生" else User.Type.TEACHER,
                                password=password  # 设置明文密码
                            )

                            # 创建NaturalPerson实例
                            NaturalPerson.objects.create(
                                user=user,
                                stu_id_dbonly=sid,
                                name=name_value,
                                gender=np_gender,
                                email=email,
                                # ... 其他字段
                            )

                            # 创建预约用户
                            Participant.objects.create(Sid=user)

                            # 添加到密码列表
                            password_list.append((sid, name_value, password))

                    # 导入结束后生成CSV
                    if password_list:
                        # 创建一个内存中的文本流
                        csv_buffer = io.StringIO()
                        csv_writer = csv.writer(csv_buffer)

                        # 写入表头
                        csv_writer.writerow(['ID', '姓名', '密码'])

                        # 写入数据行
                        for uid, name, pwd in password_list:
                            csv_writer.writerow([uid, name, pwd])

                        # 获取CSV内容
                        csv_content = csv_buffer.getvalue()
                        csv_buffer.close()

                        # 创建HTTP响应
                        response = HttpResponse(
                            csv_content, content_type='text/csv')
                        response['Content-Disposition'] = 'attachment; filename="user_password.csv"'

                        # 添加成功消息
                        messages.success(request, "Excel 导入成功！下载密码列表。")

                        return response
                    else:
                        messages.success(request, "Excel 导入成功，但未导入任何用户。")
                        return redirect("admin:app_naturalperson_changelist")

                except Exception as e:
                    messages.error(request, f"导入失败：{e}")
        else:
            form = ExcelImportForm()

        context["form"] = form
        return render(request, "admin/naturalperson_import_excel.html", context)

@admin.register(Freshman)
class FreshmanAdmin(admin.ModelAdmin):
    list_display = [
        "sid",
        "name",
        "place",
        "grade",
        "status",
    ]
    search_fields = ("sid", "name")
    list_filter = ("status", "grade", "place")


@admin.register(OrganizationType)
class OrganizationTypeAdmin(admin.ModelAdmin):
    list_display = ["otype_id", "otype_name", "incharge", "job_name_list", "control_pos_threshold"]
    search_fields = ("otype_name", "otype_id", "incharge__name", "job_name_list")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ["organization_id", "oname", "otype", "Managers"]
    search_fields = ("organization_id__username", "oname", "otype__otype_name")
    list_filter = ["otype", "status"]

    def Managers(self, obj):
        display = ''
        all_pos = sorted(Position.objects.activated().filter(
                org=obj, is_admin=True).values_list(
                    'pos', flat=True).distinct())
        for pos in all_pos:
            managers = Position.objects.activated().filter(
                org=obj, pos=pos, is_admin=True)
            if managers:
                display += f'{obj.otype.get_name(pos)}：'
                names = managers.values_list('person__name', flat=True)
                display += f"<li>{'、'.join(names)}</li>"
        if not display:
            display = '暂无'
        return mark_safe(display)
    Managers.short_description = "管理者"

    inlines = [PositionInline]

    def view_on_site(self, obj: Organization):
        return obj.get_absolute_url()

    actions = ['all_subscribe', 'all_unsubscribe']

    @as_action("设置 全部订阅")
    def all_subscribe(self, request, queryset):
        for org in queryset:
            org.unsubscribers.clear()
            org.save()
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("设置 全部不订阅")
    def all_unsubscribe(self, request, queryset):
        persons = list(NaturalPerson.objects.all().values_list('id', flat=True))
        for org in queryset:
            org.unsubscribers.set(persons)
            org.save()
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("激活", actions, update=True)
    def set_activate(self, request, queryset):
        queryset.update(status=True)
        return self.message_user(request, '修改成功!')

    @as_action("失效", actions, update=True)
    def set_disabled(self, request, queryset):
        queryset.update(status=False)
        return self.message_user(request, '修改成功!')


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ["person", "org", "pos", "pos_name", "year", "semester", "is_admin"]
    search_fields = ("person__name", "org__oname", 'org__otype__otype_name')
    list_filter = ('year', 'semester','is_admin', 'org__otype', 'pos')
    autocomplete_fields = ['person', 'org']

    def pos_name(self, obj):
        return obj.org.otype.get_name(obj.pos)
    pos_name.short_description = "职务名称"

    actions = ['demote', 'promote', 'to_member', 'to_manager', 'set_admin', 'set_not_admin']

    @as_action("职务等级 增加(降职)", update=True)
    def demote(self, request, queryset):
        for pos in queryset:
            pos.pos += 1
            pos.save()
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("职务等级 降低(升职)", update=True)
    def promote(self, request, queryset):
        for pos in queryset:
            pos.pos = max(0, pos.pos - 1)
            pos.save()
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("设为成员", update=True)
    def to_member(self, request, queryset):
        for pos in queryset:
            pos.pos = pos.org.otype.get_length()
            pos.is_admin = False
            pos.save()
        return self.message_user(request=request,
                                 message='修改成功, 并收回了管理权限!')

    @as_action("设为负责人", update=True)
    def to_manager(self, request, queryset):
        for pos in queryset:
            pos.pos = 0
            pos.is_admin = True
            pos.save()
        return self.message_user(request=request,
                                 message='修改成功, 并赋予了管理权限!')

    @as_action("赋予 管理权限", update=True)
    def set_admin(self, request, queryset):
        queryset.update(is_admin=True)
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("收回 管理权限", update=True)
    def set_not_admin(self, request, queryset):
        queryset.update(is_admin=False)
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("延长职务年限", actions, atomic=True)
    def refresh(self, request, queryset):
        from boot.config import GLOBAL_CONFIG
        new = []
        for position in queryset:
            position: Position
            if position.year != GLOBAL_CONFIG.acadamic_year and not Position.objects.filter(
                    person=position.person, org=position.org,
                    year=GLOBAL_CONFIG.acadamic_year).exists():
                position.year = GLOBAL_CONFIG.acadamic_year
                position.pk = None
                position.save(force_insert=True)
                new.append([position.pk, position.person.get_display_name()])
        return self.message_user(request, f'修改成功!新增职务：{new}')


# @admin.register(Activity)
# class ActivityAdmin(admin.ModelAdmin):
#     list_display = ["title", 'id', "organization_id",
#                     "status", "participant_diaplay",
#                     "publish_time", "start", "end",]
#     search_fields = ('id', "title", "organization_id__oname",
#                      "current_participants",)
    
#     class ErrorFilter(admin.SimpleListFilter):
#         title = '错误状态' # 过滤标题显示为"以 错误状态"
#         parameter_name = 'wrong_status' # 过滤器使用的过滤字段
    
#         def lookups(self, request, model_admin):
#             '''针对字段值设置过滤器的显示效果'''
#             return (
#                 ('all', '全部错误状态'),
#                 ('not_waiting', '未进入 等待中 状态'),
#                 ('not_processing', '未进入 进行中 状态'),
#                 ('not_end', '未进入 已结束 状态'),
#                 ('review_end', '已结束的未审核'),
#                 ('normal', '正常'),
#             )
        
#         def queryset(self, request, queryset):
#             '''定义过滤器的过滤动作'''
#             now = datetime.now()
#             error_id_set = set()
#             activate_queryset = queryset.exclude(
#                     status__in=[
#                         Activity.Status.REVIEWING,
#                         Activity.Status.CANCELED,
#                         Activity.Status.REJECT,
#                         Activity.Status.ABORT,
#                     ])
#             if self.value() in ['not_waiting', 'all', 'normal']:
#                 error_id_set.update(activate_queryset.exclude(
#                     status=Activity.Status.WAITING).filter(
#                     apply_end__lte=now,
#                     start__gt=now,
#                     ).values_list('id', flat=True))
#             if self.value() in ['not_processing', 'all', 'normal']:
#                 error_id_set.update(activate_queryset.exclude(
#                     status=Activity.Status.PROGRESSING).filter(
#                     start__lte=now,
#                     end__gt=now,
#                     ).values_list('id', flat=True))
#             if self.value() in ['not_end', 'all', 'normal']:
#                 error_id_set.update(activate_queryset.exclude(
#                     status=Activity.Status.END).filter(
#                     end__lte=now,
#                     ).values_list('id', flat=True))
#             if self.value() in ['review_end', 'all', 'normal']:
#                 error_id_set.update(queryset.filter(
#                     status=Activity.Status.REVIEWING,
#                     end__lte=now,
#                     ).values_list('id', flat=True))

#             if self.value() == 'normal':
#                 return queryset.exclude(id__in=error_id_set)
#             elif self.value() is not None:
#                 return queryset.filter(id__in=error_id_set)
#             return queryset
    
#     list_filter = (
#         "status",
#         'year', 'semester', 'category',
#         "organization_id__otype",
#         "inner", "need_checkin", "valid",
#         ErrorFilter,
#         'endbefore',
#         "publish_time", 'start', 'end',
#     )
#     date_hierarchy = 'start'

#     def participant_diaplay(self, obj):
#         return f'{obj.current_participants}/{"无限" if obj.capacity == 10000 else obj.capacity}'
#     participant_diaplay.short_description = "报名情况"

#     inlines = [ParticipationInline]

#     actions = []

#     @as_action("更新 报名人数", actions, update=True)
#     def refresh_count(self, request, queryset: QuerySet[Activity]):
#         for activity in queryset:
#             activity.current_participants = sfilter(
#                 Participation.activity, activity).filter(
#                 status__in=[
#                     Participation.AttendStatus.ATTENDED,
#                     Participation.AttendStatus.UNATTENDED,
#                     Participation.AttendStatus.APPLYSUCCESS,
#                 ]).count()
#             activity.save()
#         return self.message_user(request=request, message='修改成功!')
    
#     @as_action('设为 普通活动', actions, update=True)
#     def set_normal_category(self, request, queryset):
#         queryset.update(category=Activity.ActivityCategory.NORMAL)
#         return self.message_user(request=request, message='修改成功!')

#     @as_action('设为 课程活动', actions, update=True)
#     def set_course_category(self, request, queryset):
#         queryset.update(category=Activity.ActivityCategory.COURSE)
#         return self.message_user(request=request, message='修改成功!')

#     def _change_status(self, activity, from_status, to_status):
#         from app.activity_utils import changeActivityStatus
#         changeActivityStatus(activity.id, from_status, to_status)
#         if remove_job(f'activity_{activity.id}_{to_status}'):
#             return '修改成功, 并移除了定时任务!'
#         else:
#             return '修改成功!'

#     @as_action("进入 等待中 状态", actions, single=True)
#     def to_waiting(self, request, queryset):
#         _from, _to = Activity.Status.APPLYING, Activity.Status.WAITING
#         msg = self._change_status(queryset[0], _from, _to)
#         return self.message_user(request, msg)
    
#     @as_action("进入 进行中 状态", actions, single=True)
#     def to_processing(self, request, queryset):
#         _from, _to = Activity.Status.WAITING, Activity.Status.PROGRESSING
#         msg = self._change_status(queryset[0], _from, _to)
#         return self.message_user(request, msg)
    
#     @as_action("进入 已结束 状态", actions, single=True)
#     def to_end(self, request, queryset):
#         _from, _to = Activity.Status.PROGRESSING, Activity.Status.END
#         msg = self._change_status(queryset[0], _from, _to)
#         return self.message_user(request, msg)

#     @as_action("取消 定时任务", actions)
#     def cancel_scheduler(self, request, queryset):
#         success_list = []
#         failed_list = []
#         CANCEL_STATUSES = [
#             'remind',
#             Activity.Status.END,
#             Activity.Status.PROGRESSING,
#             Activity.Status.WAITING,
#         ]
#         for activity in queryset:
#             failed_statuses = []
#             for status in CANCEL_STATUSES:
#                 if not remove_job(f'activity_{activity.id}_{status}'):
#                     failed_statuses.append(status)
#             if failed_statuses:
#                 if len(failed_statuses) != len(CANCEL_STATUSES):
#                     failed_list.append(f'{activity.id}: {",".join(failed_statuses)}')
#                 else:
#                     failed_list.append(f'{activity.id}')
#             else:
#                 success_list.append(f'{activity.id}')
        
#         msg = f'成功取消{len(success_list)}项活动的定时任务!' if success_list else '未能完全取消任何任务'
#         if failed_list:
#             msg += f'\n{len(failed_list)}项活动取消失败：\n{";".join(failed_list)}'
#         return self.message_user(request=request, message=msg)


# @admin.register(Participation)
# class ParticipationAdmin(admin.ModelAdmin):
#     _m = Participation
#     _act = _m.activity
#     list_display = ['id', f(_act), f(_m.person), f(_m.status)]
#     search_fields = ['id', f(_act, 'id'), f(_act, Activity.title),
#                      f(_m.person, NaturalPerson.name)]
#     list_filter = [
#         f(_m.status), f(_act, Activity.category),
#         f(_act, Activity.year), f(_act, Activity.semester),
#     ]


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["id", "receiver", "sender", "title", "start_time"]
    search_fields = ('id', "receiver__username", "sender__username", 'title')
    list_filter = ('start_time', 'status', 'typename', "finish_time")

    actions = [
        'set_delete',
        'republish',
        'republish_bulk_at_promote', 'republish_bulk_at_message',
        ]

    @as_action("设置状态为 删除", update=True)
    def set_delete(self, request, queryset):
        queryset.update(status=Notification.Status.DELETE)
        return self.message_user(request=request,
                                 message='修改成功!')

    @as_action("重发 单个通知")
    def republish(self, request, queryset):
        if len(queryset) != 1:
            return self.message_user(request=request,
                                     message='一次只能重发一个通知!',
                                     level='error')
        notification = queryset[0]
        from app.extern.wechat import publish_notification, WechatApp
        if not publish_notification(
            notification,
            app=WechatApp.NORMAL,
            ):
            return self.message_user(request=request,
                                     message='发送失败!请检查通知内容!',
                                     level='error')
        return self.message_user(request=request,
                                 message='已成功定时,将发送至默认窗口!')
    
    def republish_bulk(self, request, queryset, app):
        if not request.user.is_superuser:
            return self.message_user(request=request,
                                     message='操作失败,没有权限,请联系老师!',
                                     level='warning')
        if len(queryset) != 1:
            return self.message_user(request=request,
                                     message='一次只能选择一个通知!',
                                     level='error')
        bulk_identifier = queryset[0].bulk_identifier
        if not bulk_identifier:
            return self.message_user(request=request,
                                     message='该通知不存在批次标识!',
                                     level='error')
        try:
            from app.extern.wechat import publish_notifications
        except Exception as e:
            return self.message_user(request=request,
                                     message=f'导入失败, 原因: {e}',
                                     level='error')
        if not publish_notifications(
            filter_kws={'bulk_identifier': bulk_identifier},
            app=app,
            ):
            return self.message_user(request=request,
                                     message='发送失败!请检查通知内容!',
                                     level='error')
        return self.message_user(request=request,
                                 message=f'已成功定时!标识为{bulk_identifier}')
    republish_bulk.short_description = "错误的重发操作"

    @as_action("重发 所在批次 于 订阅窗口")
    def republish_bulk_at_promote(self, request, queryset):
        try:
            from app.extern.wechat import WechatApp
            app = WechatApp._PROMOTE
        except Exception as e:
            return self.message_user(request=request,
                                     message=f'导入失败, 原因: {e}',
                                     level='error')
        return self.republish_bulk(request, queryset, app)

    @as_action("重发 所在批次 于 消息窗口")
    def republish_bulk_at_message(self, request, queryset):
        try:
            from app.extern.wechat import WechatApp
            app = WechatApp._MESSAGE
        except Exception as e:
            return self.message_user(request=request,
                                     message=f'导入失败, 原因: {e}',
                                     level='error')
        return self.republish_bulk(request, queryset, app)


@admin.register(Help)
class HelpAdmin(admin.ModelAdmin):
    list_display = ["id", "title"]


@admin.register(Wishes)
class WishesAdmin(admin.ModelAdmin):
    list_display = ["id", "text", 'time', "background_display"]
    list_filter = ('time', 'background')
    
    def background_display(self, obj):
        return mark_safe(f'<span style="color: {obj.background};"><strong>{obj.background}</strong></span>')
    background_display.short_description = "背景颜色"

    actions = ['change_color']

    @as_action("随机设置背景颜色", superuser=False, update=True)
    def change_color(self, request, queryset):
        for wish in queryset:
            wish.background = Wishes.rand_color()
            wish.save()
        return self.message_user(request=request,
                                 message='修改成功!已经随机设置了背景颜色!')
        

@admin.register(ModifyRecord)
class ModifyRecordAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "usertype", "name", 'time']
    search_fields = ('id', "user__username", "name")
    list_filter = ('time', 'usertype')

    actions = ['get_rank']

    @as_action("查询排名", superuser=False)
    def get_rank(self, request, queryset):
        if len(queryset) != 1:
            return self.message_user(
                request=request, message='一次只能查询一个用户的排名!', level='error')
        try:
            record = queryset[0]
            usertype = record.usertype
            records = ModifyRecord.objects.filter(
                user=record.user, usertype=usertype)
            first = records.order_by('time')[0]
            rank = ModifyRecord.objects.filter(
                usertype=usertype,
                time__lte=first.time,
                ).values('user').distinct().count()
            return self.message_user(request=request,
                                    message=f'查询成功: {first.name}的排名为{rank}!')
        except Exception as e:
            return self.message_user(request=request,
                                    message=f'查询失败: {e}!', level='error')


@admin.register(ModifyPosition)
class ModifyPositionAdmin(admin.ModelAdmin):
    list_display = ["id", "person", "org", "apply_type", "status"]
    search_fields = ("org__oname", "person__name")
    list_filter = ("apply_type", 'status', "org__otype", 'time', 'modify_time',)


@admin.register(ModifyOrganization)
class ModifyOrganizationAdmin(admin.ModelAdmin):
    list_display = ["id", "oname", "otype", "pos", "get_poster_name", "status"]
    search_fields = ("id", "oname", "otype__otype_name", "pos__username",)
    list_filter = ('status', "otype", 'time', 'modify_time',)
    actions = []
    ModifyOrganization.get_poster_name.short_description = "申请者"

    @as_action("同意申请", actions, 'change', update = True)
    def approve_requests(self, request, queryset: QuerySet['ModifyOrganization']):
        for application in queryset:
            accept_modifyorg_submit(application)
        self.message_user(request, '操作成功完成！')


admin.site.register(OrganizationTag)
admin.site.register(Comment)
admin.site.register(CommentPhoto)
# admin.site.register(ActivitySummary)
