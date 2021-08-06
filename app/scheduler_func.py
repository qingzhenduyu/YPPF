from apscheduler.schedulers.background import BackgroundScheduler
from django.dispatch.dispatcher import receiver
from django_apscheduler.jobstores import DjangoJobStore, register_events, register_job

from django.db.models import F
from django.http import JsonResponse, HttpResponse  # Json响应
from django.shortcuts import render, redirect  # 网页render & redirect
from django.urls import reverse
from datetime import datetime, timedelta, timezone, time, date
from django.db import transaction  # 原子化更改数据库

from app.models import Organization, NaturalPerson, YQPoint_Distribute, TransferRecord, User
from app.wechat_send import base_send_wechat
from app.forms import YQPoint_DistributionForm

# 定时任务生成器
scheduler = BackgroundScheduler()
scheduler.add_jobstore(DjangoJobStore(), "default")

def distribute_YQPoint_to_User(proposer, recipients, YQPoints, trans_time):
    '''
        由proposer账户(默认为一个组织账户)，向每一个在recipidents中的账户中发起数额为YQPoints的转账
        并且自动生成默认为ACCEPTED的转账记录以便查阅

        可能在之后也有用
    '''
    try:
        assert proposer.YQPoint >= recipients.count() * YQPoints
    except:
        # 说明此时proposer账户的元气值不足
        print(f"由{proposer}向自然人{recipients[:3]}...等{recipients.count()}个用户发放元气值失败，原因可能是{proposer}的元气值剩余不足")
    try:
        is_nperson = isinstance(recipients[0], NaturalPerson) # 不为自然人则为组织
    except:
        print("没有转账对象！")
        return
    # 更新元气值
    recipients.update(YQPoint=F('YQPoint') + YQPoints)
    proposer.YQPoint -= recipients.count() * YQPoints
    proposer.save()
    # 生成转账记录
    trans_msg = f"{proposer}向您发放了{YQPoints}元气值，请查收！"
    transfer_list = [TransferRecord(
            proposer=proposer.organization_id,
            recipient=(recipient.person_id if is_nperson else recipient.organization_id),
            amount=YQPoints,
            start_time=trans_time,
            finish_time=trans_time,
            message=trans_msg,
            status=TransferRecord.TransferStatus.ACCEPTED
    ) for recipient in recipients]
    TransferRecord.objects.bulk_create(transfer_list)
    

def distribute_YQPoint(distributer):
    trans_time = distributer.start_time

    # 没有问题，找到要发放元气值的人和组织
    per_to_dis = NaturalPerson.objects.activated().filter(
        YQPoint__lte=distributer.per_max_dis_YQPoint)
    org_to_dis = Organization.objects.activated().filter(
        YQPoint__lte=distributer.org_max_dis_YQPoint).exclude(oname="元培学院")
    # 由学院账号给大家发放
    YPcollege = Organization.objects.get(oname="元培学院")

    distribute_YQPoint_to_User(proposer=YPcollege, recipients=per_to_dis, YQPoints=distributer.per_YQPoints, trans_time=trans_time)
    distribute_YQPoint_to_User(proposer=YPcollege, recipients=org_to_dis, YQPoints=distributer.org_YQPoints, trans_time=trans_time)
    end_time = datetime.now()
    
    debug_msg = f"已向{per_to_dis.count()}个自然人和{org_to_dis.count()}个组织转账，用时{(end_time - trans_time).seconds}s,{(end_time - trans_time).microseconds}microsecond\n"
    print(debug_msg)


def add_YQPoints_distribute(dtype):
    '''
        用于注册已知type=dtype的发放元气值的实例
        每种类型（临时发放、每周发放、每两周发放）都必须只有一个正在应用的实例
    '''
    try:
        distributer = YQPoint_Distribute.objects.get(type=dtype, status=True)
    except Exception as e:
        print(f"按类型{dtype}发放元气值失败，原因可能是没有状态为YES或者有多个状态为YES的发放实例\n" + str(e))
    if dtype == YQPoint_Distribute.DistributionType.TEMPORARY:
        # 说明此时是临时发放
        scheduler.add_job(distribute_YQPoint, "date", id="temporary_YQP_distribute", 
                        run_date=distributer.start_time, args = [distributer])
    else:
        # 说明此时是定期发放
        scheduler.add_job(distribute_YQPoint, "interval", id=f"{dtype}weeks_interval_YQP_distribute", 
                        weeks=distributer.type, next_run_time=distributer.start_time, args=[distributer])


def YQPoint_Distributions(request):
    context = dict()
    context['YQPoint_Distributions'] = YQPoint_Distribute.objects.all()
    return render(request, "YQPoint_Distributions.html", context)


def YQPoint_Distribution(request, dis_id):  
    dis = YQPoint_Distribute.objects.get(id=dis_id)
    dis_form = YQPoint_DistributionForm()
    if request.method == 'POST':
        dis_form = YQPoint_DistributionForm(request.POST)
        if dis_form.is_valid():
            dis_form = YQPoint_DistributionForm(request.POST, instance=dis)
            dis_form.save()
            if dis.status == True:
                # 在这里注册scheduler
                add_YQPoints_distribute(dis.type)
    context = dict()
    context["dis"] = dis
    context["dis_form"] = dis_form
    return render(request, "YQPoint_Distribution.html", context)