# -*- coding: utf-8 -*-

import json
import sys
import os
import subprocess
import time
import threading
import requests
import logging
from logging import getLogger, Formatter, DEBUG
from logging.handlers import TimedRotatingFileHandler

import config_api
from alert.guardian_alert import GuardianAlert
from alert.alert_util import AlertException

# TODO:
# from contacts import contacts


import spark_checker

# TODO:
# start application concurrently

log = getLogger()
log_file = os.path.abspath("logs/guardian.log")
rotate_handler = TimedRotatingFileHandler(log_file, when='h', interval=24,
                                          backupCount=7)

stream_handler = logging.StreamHandler()
format_str = ('%(asctime)s %(levelname)s '
              '[%(module)s %(filename)s:%(lineno)d] %(message)s')
formatter = Formatter(format_str)
rotate_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

log.addHandler(rotate_handler)
log.setLevel(DEBUG)


def set_config_default(config):
    if 'node_name' not in config:
        config['node_name'] = u'my_guardian'


def get_args_check(args):
    f = open(args, 'r')
    try:
        config = json.load(f)
    except ValueError as e:
        log.error(repr(e))
        raise ValueError('Config file is not a valid json')

    set_config_default(config)

    config = _get_active_app_config(config)

    return config


class ThreadCheck(threading.Thread):
    def __init__(self, t_name, file_path, alert_client):
        self.path = file_path
        self.alert_client = alert_client
        threading.Thread.__init__(self, name=t_name)

    def run(self):
        command_check(self.path, self.alert_client)


def command_check(file_path, alert_client):
    """

    :param file_path: Config file path.
    :param alert_client: Member of GuardianAlert
    :return:
    """
    logging.info("Starting to check applications")

    while True:
        config = get_args_check(file_path)
        check_impl(config, alert_client)
        time.sleep(config['check_interval'])


class GuardianError(Exception):
    pass


class NoAvailableYarnRM(GuardianError):
    pass


class NoActiveYarnRM(GuardianError):
    pass


class CannotGetClusterApps(GuardianError):
    pass


def _get_active_app_config(config):
    apps = config['apps']
    del_list = []
    for i in range(len(apps)):
        app = apps[i]
        if 'active' in app.keys() and app['active'] is False:
            del_list.insert(0, i)

    for i in del_list:
        del apps[i]

    return config


def _get_yarn_active_rm(hosts, timeout=10):
    """Find active yarn resource manager.
    """

    active_rm = None
    available_hosts = len(hosts)
    for host in hosts:
        url = 'http://{host}/ws/v1/cluster/info'.format(host=host)
        try:
            resp = requests.get(url, timeout=timeout)
        except requests.exceptions.ConnectTimeout as e:
            available_hosts -= 1
            continue

        if resp.status_code != 200:
            available_hosts -= 1
            continue

        cluster_info = resp.json()

        if cluster_info['clusterInfo']['haState'].lower() == "active":
            active_rm = host
            break

    if available_hosts == 0:
        raise NoAvailableYarnRM

    if active_rm is None:
        raise NoActiveYarnRM

    logging.debug('Picked up Yarn active resource manager:' + active_rm)

    return active_rm


def _request_yarn(hosts, timeout=10):
    active_rm = _get_yarn_active_rm(hosts)

    url = 'http://{host}/ws/v1/cluster/apps?states=accepted,running'.format(
        host=active_rm)
    resp = requests.get(url, timeout=timeout)
    if resp.status_code != 200:
        raise CannotGetClusterApps()

    stats = resp.json()

    if len(stats.keys()) == 0:
        raise ValueError('Cannot not get yarn application stats')

    return stats, active_rm


def check_impl(args, alert_client):
    yarn_active_rm = None
    retry = 0
    while retry < 3:
        try:
            j, yarn_active_rm = _request_yarn(args['yarn']['api_hosts'])
            break

        except (ValueError, NoAvailableYarnRM, NoActiveYarnRM):
            logging.warning("Failed to send request to yarn resource manager, "
                            "retry")
            retry += 1

    if retry >= 3:
        logging.error(
            "Failed to send request to yarn resource manager, host config: " +
            ', '.join(args['yarn']['api_hosts']))
        subject = 'Guardian'
        objects = 'Yarn RM'
        content = 'Failed to send request to yarn resource manager.'
        alert_client.send_alert("ERROR", subject, objects, content)

        return

    if j['apps'] is None:
        logging.info("There is no app in yarn.")
        running_apps = []
    else:
        running_apps = j['apps']['app']

    app_map = {}
    for app in running_apps:
        key = app['name']
        if key not in app_map:
            app_map[key] = []

        app_map[key].append(app)

    not_running_apps = []
    for app_config in args['apps']:
        app_name = app_config['app_name']

        apps = None
        try:
            apps = app_map[app_name]
        except KeyError:
            apps = []

        actual_app_num = len(apps)
        expected_app_num = 1
        try:
            expected_app_num = app_config['app_num']
        except KeyError:
            pass

        if actual_app_num < expected_app_num:
            not_running_apps.append(app_name)
            continue

        # app is running but not in expected number
        if actual_app_num > expected_app_num:
            subject = 'Guardian'
            objects = app_name
            content = 'Unexpected running app number, expected/actual: {expected}/{actual}'.format(
                expected=app_config['app_num'], actual=len(apps))
            alert_client.send_alert("ERROR", subject, objects, content)
            continue

        # specific type of checker has been set
        if 'check_type' in app_config and 'check_options' in app_config:

            config = {
                'app': app_config,
                'yarn': {
                    'active_rm': yarn_active_rm
                },
                'node_name': args['node_name'],
            }

            if app_config['check_type'] == 'spark':
                spark_checker.check(apps, config, alert_client)

    if len(not_running_apps) == 0:
        logging.info("There is no application need to be started.")
        return

    alert_not_running_apps(not_running_apps, args['apps'], alert_client)


def alert_not_running_apps(app_names, app_configs, alert_client):
    for app_name in app_names:

        subject = 'Guardian'
        objects = app_name
        content = ('App is not running or less than expected number of '
                   'running instance, will restart.')
        alert_client.send_alert("ERROR", subject, objects, content)

        app_info = filter(lambda x: x['app_name'] == app_name, app_configs)
        raw_cmd = app_info[0]['start_cmd']
        cmd = raw_cmd.split()  # split by whitespace to comand and arguments for popen

        retry = 0
        while retry < 3:
            try:
                p = subprocess.Popen(cmd)
            except OSError:
                # probably os cannot find the start command.
                log.error("Invalid start command: " + raw_cmd)
                retry += 1
                continue

            output, err = p.communicate()

            if err is not None:
                print err
                retry += 1
                continue
            else:
                print output
                break

        if retry >= 3:

            logging.info("Alert sms after failed 3 times.")
            subject = 'Guardian'
            objects = app_name
            content = 'Failed to start yarn app after 3 times.'
            alert_client.send_alert("ERROR", subject, objects, content)

    logging.info("Finished checking applications")


def get_args_inspect(args):
    """
    args:
        filter: only support "app_name"
        value: only support regular expression
    """

    if len(args) != 3:
        raise ValueError("Invalid argument number")

    f = open(args[0], 'r')
    try:
        config = json.load(f)
    except ValueError as e:
        log.error(repr(e))
        raise ValueError('Config file is not a valid json')

    if args[1] != 'app_name':
        raise ValueError("Invalid Filter, only support \"app_name\"")

    args_map = {
        'config': config,
        'filter': args[1],
        'value': args[2],
    }

    return args_map


def command_inspect(args):
    logging.info("Starting to inspect applications")

    import re

    def match(s):

        pattern = args['value']
        m = re.search(pattern, s)
        return True if m is not None else False

    j, active_rm = _request_yarn(args['config']['yarn']['api_hosts'])

    if j['apps'] is None:
        logging.info("There's no app in yarn")
        return

    running_apps = j['apps']['app']

    running_app_names = map(lambda x: x['name'], running_apps)
    running_app_names = filter(match, running_app_names)
    running_app_names = set(running_app_names)

    config = args['config']

    configured_apps = map(lambda x: x['app_name'], config['apps'])
    configured_apps = set(configured_apps)

    for app_name in running_app_names - configured_apps:
        config['apps'].append({
            'app_name': app_name,
            'start_cmd': 'TODO',
            'app_num': 1
        })

    print json.dumps(config, indent=4)

    logging.info("Finished inspecting applications, please check config.")


if __name__ == '__main__':

    import ntpath

    executable = ntpath.basename(sys.argv[0])
    if len(sys.argv[1:]) < 1:
        print "usage:", executable, "<command> <command_args>"
        print "  commands:"
        print "    - check <config_file>"
        print "      example:", executable, "check ./config.json"
        print ""
        print "    - inspect <config_file> <filter> <value>"
        print "          * filter: app_name"
        print "          * value: any regular expression"
        print "      example:", executable, "inspect ./config.json app_name waterdrop_"
        print ""
        sys.exit(-1)

    command = sys.argv[1]

    try:

        if command == 'check':
            if len(sys.argv[2:]) != 1:
                raise ValueError('Invalid argument number')

            config = get_args_check(sys.argv[2])

            alert_client = GuardianAlert(config["alert_manager"])
            t = ThreadCheck('check', sys.argv[2], alert_client)
            t.setDaemon(True)
            t.start()

            port = 5000
            if 'port' in config:
                port = config['port']

            app = config_api.app
            app.config['config_name'] = sys.argv[2]

            app.run(host='0.0.0.0', port=port)

        elif command == 'inspect':
            config = get_args_inspect(sys.argv[2:])
            command_inspect(config)

        else:
            raise ValueError("Unsupported Command:" + command)

    except KeyboardInterrupt as e:
        logging.info("Exiting. Bye")
        sys.exit(0)
