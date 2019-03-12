import os.path


from django.db.models import Count
from django.conf import settings
from django.utils.timezone import now

from awx.conf.license import get_license
from awx.main.utils import (get_awx_version, get_ansible_version,
                            get_custom_venv_choices, camelcase_to_underscore)
from awx.main import models
from django.contrib.sessions.models import Session
from awx.main.analytics import register


# This module is used to define metrics collected by awx.main.analytics.gather()
# Each function is decorated with a key name, and should return a data
# structure that can be serialized to JSON
#
# @register('something')
# def something(since):
#     # the generated archive will contain a `something.json` w/ this JSON
#     return {'some': 'json'}
#
# All functions - when called - will be passed a datetime.datetime object,
# `since`, which represents the last time analytics were gathered (some metrics
# functions - like those that return metadata about playbook runs, may return
# data _since_ the last report date - i.e., new data in the last 24 hours)


@register('config')
def config(since):
    license_info = get_license(show_key=False)
    return {
        'system_uuid': settings.SYSTEM_UUID,
        'tower_url_base': settings.TOWER_URL_BASE,
        'tower_version': get_awx_version(),
        'ansible_version': get_ansible_version(),
        'license_type': license_info.get('license_type', 'UNLICENSED'),
        'free_instances': license_info.get('free instances', 0),
        'license_expiry': license_info.get('time_remaining', 0),
        'authentication_backends': settings.AUTHENTICATION_BACKENDS,
        'logging_aggregators': settings.LOG_AGGREGATOR_LOGGERS
    }


@register('counts')
def counts(since):
    counts = {}
    for cls in (models.Organization, models.Team, models.User,
                models.Inventory, models.Credential, models.Project,
                models.JobTemplate, models.WorkflowJobTemplate, models.Host,
                models.Schedule, models.CustomInventoryScript,
                models.NotificationTemplate):
        counts[camelcase_to_underscore(cls.__name__)] = cls.objects.count()

    venvs = get_custom_venv_choices()
    counts['custom_virtualenvs'] = len([
        v for v in venvs
        if os.path.basename(v.rstrip('/')) != 'ansible'
    ])

    inv_counts = dict(models.Inventory.objects.order_by().values_list('kind').annotate(Count('kind')))
    inv_counts['normal'] = inv_counts[''] # rename '' to 'normal'
    counts['inventories'] = inv_counts
    
    counts['active_host_count'] = models.Host.objects.active_count()   
    active_sessions = Session.objects.filter(expire_date__gte=now()).count()
    api_sessions = models.UserSessionMembership.objects.select_related('session').filter(session__expire_date__gte=now()).count()
    channels_sessions = active_sessions - api_sessions
    counts['active_sessions'] = active_sessions
    counts['active_api_sessions'] = api_sessions
    counts['active_channels_sessions'] = channels_sessions
    counts['running_jobs'] = models.UnifiedJob.objects.filter(status__in=('running', 'waiting',)).count()
    return counts

    
@register('org_counts')
def org_counts(since):
    counts = {}
    for org in models.Organization.objects.annotate(num_users=Count('member_role__members', distinct=True), 
                                                    num_teams=Count('teams', distinct=True)).values('name', 'id', 'num_users', 'num_teams'):
        counts[org['id']] = {'name': org['name'],
                            'users': org['num_users'],
                            'teams': org['num_teams']
                            }
    return counts
    
    
@register('cred_type_counts')
def cred_type_counts(since):
    counts = {}
    for cred_type in models.CredentialType.objects.annotate(num_credentials=Count('credentials', distinct=True)):  
        counts[cred_type.id] = {'name': cred_type.name,
                                'credential_count': cred_type.num_credentials
                                }
    return counts
    
    
@register('inventory_counts')
def inventory_counts(since):
    counts = {}
    for inv in models.Inventory.objects.filter(kind='').annotate(num_sources=Count('inventory_sources', distinct=True), 
                                                 num_hosts=Count('hosts', distinct=True)).only('id', 'name', 'kind'):
        counts[inv.id] = {'name': inv.name,
                          'kind': inv.kind,
                          'hosts': inv.num_hosts,
                          'sources': inv.num_sources
                          }

    for smart_inv in models.Inventory.objects.filter(kind='smart'):
        counts[smart_inv.id] = {'name': smart_inv.name,
                          'kind': smart_inv.kind,
                          'num_hosts': smart_inv.hosts.count(),
                          'num_sources': smart_inv.inventory_sources.count()
                          }
    return counts


@register('projects_by_scm_type')
def projects_by_scm_type(since):
    counts = dict(
        (t[0] or 'manual', 0)
        for t in models.Project.SCM_TYPE_CHOICES
    )
    for result in models.Project.objects.values('scm_type').annotate(
        count=Count('scm_type')
    ).order_by('scm_type'):
        counts[result['scm_type'] or 'manual'] = result['count']
    return counts

@register('instance_info')   # maybe integrate this with job_instance_counts?
def instance_info(since):
    info = {}
    instances = models.Instance.objects.values_list('hostname').annotate().values('uuid', 'version', 'capacity', 'cpu', 'memory', 'managed_by_policy', 'hostname')
    for instance in instances:
        info = {'uuid': instance['uuid'],
                'version': instance['version'],
                'capacity': instance['capacity'],
                'cpu': instance['cpu'],
                'memory': instance['memory'],
                'managed_by_policy': instance['managed_by_policy'],
                }
    return info


@register('job_counts')
def job_counts(since):
    counts = {}
    counts['total_jobs'] = models.UnifiedJob.objects.all().count()
    counts['status'] = dict(models.UnifiedJob.objects.values_list('status').annotate(Count('status')))
    counts['launch_type'] = dict(models.UnifiedJob.objects.values_list('launch_type').annotate(Count('launch_type')))
    
    return counts
    
    
@register('job_instance_counts')
def job_instance_counts(since):         #TODO: all of these are going to need to be restrained to the last 24 hours/INSIGHTS_SCHEDULE
    counts = {}

    job_types = models.UnifiedJob.objects.values_list(
        'execution_node', 'launch_type').annotate(job_launch_type=Count('launch_type'))
    for job in job_types:
        counts.setdefault(job[0], {})[job[1]] = job[2]
        
    job_statuses = models.UnifiedJob.objects.values_list(
        'execution_node', 'status').annotate(job_status=Count('status'))
    for job in job_statuses:
        counts.setdefault(job[0], {})[job[1]] = job[2]
        
# See if we can group by status and type inside the node grouping.  ^^

    # 'jobs_running': models.UnifiedJob.objects.filter(execution_node=instance.hostname, status__in=('running', 'waiting',)).count(), # jobs in running & waiting state
    # 'jobs_total': models.UnifiedJob.objects.filter(execution_node=instance.hostname).count(),
    # ^^ try to get jobs_running and jobs_total per instance.  and uuid if easily possible  

    return counts

