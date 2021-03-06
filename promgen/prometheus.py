# Copyright (c) 2017 LINE Corporation
# These sources are released under the terms of the MIT license: see LICENSE

import collections
import datetime
import json
import logging
import re
import subprocess
import tempfile
from urllib.parse import urljoin

import pytz
from atomicwrites import atomic_write
from dateutil import parser
from django.conf import settings
from django.template.loader import render_to_string

from promgen import models, util
from promgen.celery import app as celery

logger = logging.getLogger(__name__)


def check_rules(rules):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf8') as fp:
        logger.debug('Rendering to %s', fp.name)
        # Normally we wouldn't bother saving a copy to a variable here and would
        # leave it in the fp.write() call, but saving a copy in the variable
        # means we can see the rendered output in a Sentry stacktrace
        rendered = render_rules(rules)
        fp.write(rendered)
        fp.flush()

        try:
            subprocess.check_output([
                settings.PROMGEN['rule_writer']['promtool_path'],
                'check-rules',
                fp.name
            ], stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            raise Exception(rendered + e.output.decode('utf8'))


def render_rules(rules=None):
    if rules is None:
        rules = models.Rule.objects.filter(enabled=True).prefetch_related(
            'content_object',
            'content_type',
            'overrides__content_object',
            'overrides__content_type',
            'ruleannotation_set',
            'rulelabel_set',
        )
    return render_to_string('promgen/prometheus.rule', {'rules': rules})


def render_urls():
    urls = collections.defaultdict(list)

    for url in models.URL.objects.prefetch_related(
            'project__farm__host_set',
            'project__farm',
            'project__service__shard',
            'project__service',
            'project'):
        urls[(
            url.project.name, url.project.service.name, url.project.service.shard.name,
        )].append(url.url)

    data = [{'labels': {'project': k[0], 'service': k[1], '__shard': k[2]}, 'targets': v} for k, v in urls.items()]
    return json.dumps(data, indent=2, sort_keys=True)


@celery.task
def write_urls(path=None, reload=True):
    if path is None:
        path = settings.PROMGEN['url_writer']['path']
    with atomic_write(path, overwrite=True) as fp:
        fp.write(render_urls())
    if reload:
        reload_prometheus()


def render_config(service=None, project=None):
    data = []
    for exporter in models.Exporter.objects.\
            prefetch_related(
                'project__farm__host_set',
                'project__farm',
                'project__service__shard',
                'project__service',
                'project',
                ):
        if not exporter.project.farm:
            continue
        if service and exporter.project.service.name != service.name:
            continue
        if project and exporter.project.name != project.name:
            continue
        if not exporter.enabled:
            continue

        labels = {
            '__shard': exporter.project.service.shard.name,
            'service': exporter.project.service.name,
            'project': exporter.project.name,
            'farm': exporter.project.farm.name,
            '__farm_source': exporter.project.farm.source,
            'job': exporter.job,
        }
        if exporter.path:
            labels['__metrics_path__'] = exporter.path

        hosts = []
        for host in exporter.project.farm.host_set.all():
            hosts.append('{}:{}'.format(host.name, exporter.port))

        data.append({
            'labels': labels,
            'targets': hosts,
        })
    return json.dumps(data, indent=2, sort_keys=True)


@celery.task
def write_config(path=None, reload=True):
    if path is None:
        path = settings.PROMGEN['config_writer']['path']
    with atomic_write(path, overwrite=True) as fp:
        fp.write(render_config())
    if reload:
        reload_prometheus()


@celery.task
def write_rules(path=None, reload=True):
    if path is None:
        path = settings.PROMGEN['rule_writer']['path']
    with atomic_write(path, overwrite=True) as fp:
        fp.write(render_rules())
    if reload:
        reload_prometheus()


@celery.task
def reload_prometheus():
    from promgen.signals import post_reload
    target = urljoin(settings.PROMGEN['prometheus']['url'], '/-/reload')
    response = util.post(target)
    post_reload.send(response)


def import_rules(config, default_service=None):
    # Attemps to match the pattern name="value" for Prometheus labels and annotations
    RULE_MATCH = re.compile('((?P<key>\w+)\s*=\s*\"(?P<value>.*?)\")')
    counters = collections.defaultdict(int)

    def parse_prom(text):
        if not text:
            return {}
        converted = {}
        for match, key, value in RULE_MATCH.findall(text.strip().strip('{}')):
            converted[key] = value
        return converted

    tokens = {}
    rules = []
    for line in config.split('\n'):
        line = line.strip()
        if not line:
            continue
        if line.startswith('#'):
            continue

        keyword, data = line.split(' ', 1)

        if keyword != 'ALERT':
            tokens[keyword] = data
            continue

        if keyword == 'ALERT' and 'ALERT' not in tokens:
            tokens[keyword] = data
            continue

        rules.append(tokens)
        # Start building our next rule
        tokens = {keyword: data}
    # Make sure we keep our last token after parsing all lines
    rules.append(tokens)

    for tokens in rules:
        labels = parse_prom(tokens.get('LABELS'))
        annotations = parse_prom(tokens.get('ANNOTATIONS'))

        if default_service:
            service = default_service
        else:
            try:
                service = models.Service.objects.get(name=labels.get('service', 'Default'))
            except models.Service.DoesNotExist:
                service = models.Service.default()

        rule, created = models.Rule.get_or_create(
            name=tokens['ALERT'],
            defaults={
                'clause': tokens['IF'],
                'duration': tokens['FOR'],
                'obj': service,
            }
        )

        if created:
            counters['Rules'] += 1
            for k, v in labels.items():
                models.RuleLabel.objects.create(name=k, value=v, rule=rule)
                counters['Labels'] += 1
            for k, v in annotations.items():
                models.RuleAnnotation.objects.create(name=k, value=v, rule=rule)
                counters['Annotations'] += 1

    return dict(counters)


def import_config(config, replace_shard=None):
    counters = collections.defaultdict(list)
    skipped = collections.defaultdict(list)
    for entry in config:
        if replace_shard:
            logger.debug('Importing into shard %s', replace_shard)
            entry['labels']['__shard'] = replace_shard
        shard, created = models.Shard.objects.get_or_create(
            name=entry['labels'].get('__shard', 'Default')
        )
        if created:
            logger.debug('Created shard %s', shard)
            counters['Shard'].append(shard)
        else:
            skipped['Shard'].append(shard)

        service, created = models.Service.objects.get_or_create(
            name=entry['labels']['service'],
            defaults={'shard': shard}
        )
        if created:
            logger.debug('Created service %s', service)
            counters['Service'].append(service)
        else:
            skipped['Service'].append(service)

        farm, created = models.Farm.objects.get_or_create(
            name=entry['labels']['farm'],
            defaults={'source': entry['labels'].get('__farm_source', 'pmc')}
        )
        if created:
            logger.debug('Created farm %s', farm)
            counters['Farm'].append(farm)
        else:
            skipped['Farm'].append(farm)

        project, created = models.Project.objects.get_or_create(
            name=entry['labels']['project'],
            service=service,
            defaults={'farm': farm}
        )
        if created:
            logger.debug('Created project %s', project)
            counters['Project'].append(project)
        elif project.farm != farm:
            logger.debug('Linking farm [%s] with [%s]', farm, project)
            project.farm = farm
            project.save()

        for target in entry['targets']:
            target, port = target.split(':')
            host, created = models.Host.objects.get_or_create(
                name=target,
                farm_id=farm.id,
            )

            if created:
                logger.debug('Created host %s', host)
                counters['Host'].append(host)

            exporter, created = models.Exporter.objects.get_or_create(
                job=entry['labels']['job'],
                port=port,
                project=project,
                path=entry['labels'].get('__metrics_path__', '')
            )

            if created:
                logger.debug('Created exporter %s', exporter)
                counters['Exporter'].append(exporter)

    return counters, skipped


def silence(labels, duration=None, **kwargs):
    '''
    Post a silence message to Alert Manager
    Duration should be sent in a format like 1m 2h 1d etc
    '''
    if duration:
        start = datetime.datetime.now(datetime.timezone.utc)
        if duration.endswith('m'):
            end = start + datetime.timedelta(minutes=int(duration[:-1]))
        elif duration.endswith('h'):
            end = start + datetime.timedelta(hours=int(duration[:-1]))
        elif duration.endswith('d'):
            end = start + datetime.timedelta(days=int(duration[:-1]))
        else:
            raise Exception('Unknown time modifier')
        kwargs['endsAt'] = end.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    else:
        local_timezone = pytz.timezone(settings.PROMGEN.get('timezone', 'UTC'))
        for key in ['startsAt', 'endsAt']:
            kwargs[key] = parser.parse(kwargs[key])\
                .replace(tzinfo=local_timezone)\
                .astimezone(pytz.utc)\
                .strftime('%Y-%m-%dT%H:%M:%S.000Z')

    kwargs['matchers'] = [{
        'name': name,
        'value': value,
        'isRegex': True if value.endswith("*") else False
    } for name, value in labels.items()]
    logger.debug('Sending silence for %s', kwargs)
    url = urljoin(settings.PROMGEN['alertmanager']['url'], '/api/v1/silences')
    util.post(url, json=kwargs).raise_for_status()
