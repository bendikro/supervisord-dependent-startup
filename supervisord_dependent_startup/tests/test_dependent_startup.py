from __future__ import print_function

import logging
import os
import unittest

from parameterized import param, parameterized
from supervisor.xmlrpc import Faults, xmlrpclib

from .. import supervisord_dependent_startup as plugin
from . import common, config_utils, DependentStartupError, get_all_configs, ServiceOptions
from .common import mock, StdinManualEventsWrapper
from .helpers import LogCapturePrintable
from .log_utils import plugin_logger_name, plugin_tests_logger_name  # noqa: F401
from .utils import cprint  # noqa: F401

log = logging.getLogger(plugin_tests_logger_name)


class DependentStartupRunTests(unittest.TestCase):

    @parameterized.expand([(None,), (0,), (2,), (4,)])
    def test_run_ignores_main_return(self, result):
        with mock.patch.object(plugin, 'main', return_value=result) as main:
            with LogCapturePrintable() as logs:
                with self.assertRaises(SystemExit) as raised:
                    plugin.run()
        self.assertEqual(0, raised.exception.code)
        main.assert_called_once_with()
        logs.check()

    @parameterized.expand([
        ('shutdown', xmlrpclib.Fault(Faults.SHUTDOWN_STATE, 'SHUTDOWN_STATE'), 0),
        ('generic_fault', xmlrpclib.Fault(Faults.BAD_NAME, 'bad process name'), 3),
        ('shutdown_text_only', xmlrpclib.Fault(Faults.BAD_NAME, 'SHUTDOWN_STATE'), 3),
        ('ordinary_exception', RuntimeError('SHUTDOWN_STATE'), 3),
        ('system_exit_zero', SystemExit(0), 3),
        ('system_exit_nonzero', SystemExit(2), 3),
        ('keyboard_interrupt', KeyboardInterrupt(), 3),
        ('base_exception', BaseException(), 3),
    ])
    def test_run_exception(self, name, error, exit_code):
        with mock.patch.object(plugin, 'main', side_effect=error) as main:
            with LogCapturePrintable() as logs:
                with self.assertRaises(SystemExit) as raised:
                    plugin.run()
        self.assertEqual(exit_code, raised.exception.code)
        main.assert_called_once_with()
        if exit_code == 0:
            logs.check()
        else:
            logs.check((plugin_logger_name, 'ERROR', 'Error occured:'))
            self.assertIs(type(error), logs.records[0].exc_info[0])
            self.assertIs(error, logs.records[0].exc_info[1])
            self.assertIsNotNone(logs.records[0].exc_info[2])


class DependentStartupBasicTests(common.DependentStartupWithoutEventListenerTestsBase):

    def setUp(self):
        super(DependentStartupBasicTests, self).setUp()
        # May not be needed
        os.environ['SUPERVISOR_SERVER_URL'] = "unix:///var/tmp/supervisor.sock"

    def test_get_all_configs(self):
        self.write_supervisord_config()
        service_conf, rendered = self.add_service_file("testservice")
        configs = get_all_configs(self.supervisor_conf)
        expected = [self.supervisor_conf, service_conf]
        self.assertEqual(expected, configs)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_service_handler_is_service_done(self):
        sname = 'consul'
        self.write_supervisord_config()
        self.add_test_service(sname, self.options)
        self.add_test_service('slurmd', self.options, dependent_startup_wait_for="consul:running", priority=10)
        self.add_test_service('slurmd2', self.options, dependent_startup_wait_for="consul:running slurmd:running")

        self.setup_eventlistener()

        self.monitor.run()
        handler = self.monitor.services_handler
        service = handler._services[sname]

        self.assertEqual([('consul', 'STOPPED')], handler.get_service_states(service))
        self.assertTrue(handler.is_startable(service))
        self.assertFalse(handler.is_service_done(service))

        self.monitor_listen_on_events(event_procs=['dependentstartup', 'dependentstartup'])
        self.monitor_listen_on_events(event_procs=['consul'])

        self.assertEqual([('consul', 'RUNNING')], handler.get_service_states(service))
        self.assertFalse(handler.is_startable(service))
        self.assertFalse(handler.is_service_done(service))
        self.monitor_listen_on_events(event_procs=['consul'])
        self.assertTrue(handler.is_service_done(service))

        self.monitor_run_and_listen_until_no_more_events(run=False)

    def test_run_plugin(self):
        self.write_supervisord_config()
        self.add_test_service('consul', self.options)
        self.add_test_service('slurmd', self.options, dependent_startup_wait_for="consul:running", priority=10)
        self.add_test_service('slurmd2', self.options, dependent_startup_wait_for="consul:running slurmd:running")

        self.setup_eventlistener()
        self.monitor_run_and_listen_until_no_more_events()
        # self.print_procs()
        expected_procs = ['consul', 'slurmd', 'slurmd2']
        self.assertEqual(expected_procs, self.processes_started)
        self.assertStateProcsRunning(expected_procs)

    def test_run_ping_example(self):
        self.write_supervisord_config()

        self.add_test_service('ping', self.options, cmd="/bin/ping -c 1 www.google.com", startsecs=0)
        self.add_test_service('sleep', self.options, cmd="/bin/sleep 15", startsecs=5,
                              dependent_startup_wait_for="ping:exited", autorestart=True)
        self.add_test_service('ping2', self.options, cmd="/bin/ping -c 1 www.google.com", startsecs=0,
                              dependent_startup_wait_for="sleep:running")
        self.add_test_service('ping3', self.options, cmd="/bin/ping -c 1 www.google.com", startsecs=0,
                              dependent_startup_wait_for="ping2:exited")

        self.setup_eventlistener()
        self.monitor_run_and_listen_until_no_more_events()

    def test_run_ping_example_running(self):
        self.write_supervisord_config()

        self.add_test_service('ping', self.options, cmd="/bin/ping -i 1 -c 2 www.google.com", startsecs=0)
        self.add_test_service('sleep', self.options, cmd="/bin/sleep 10", startsecs=5,
                              dependent_startup_wait_for="ping:running", autorestart=True)
        self.add_test_service('ping2', self.options, cmd="/bin/ping -i 1 -c 2 www.google.com", startsecs=0,
                              dependent_startup_wait_for="sleep:running")
        self.add_test_service('ping3', self.options, cmd="/bin/ping -i 1 -c 2 www.google.com", startsecs=0,
                              dependent_startup_wait_for="ping2:running")

        self.setup_eventlistener()
        self.monitor_run_and_listen_until_no_more_events()

    def test_run_ping_example_immedate_exit(self):
        self.write_supervisord_config()

        self.add_test_service('ping', self.options, cmd="/bin/ping -c 1 www.google.com", startsecs=0)
        self.add_test_service('sleep', self.options, cmd="/bin/sleep 10", startsecs=5,
                              dependent_startup_wait_for="ping:running", autorestart=True)
        self.add_test_service('ping2', self.options, cmd="/bin/ping -c 1 www.google.com", startsecs=0,
                              dependent_startup_wait_for="sleep:running")
        self.add_test_service('ping3', self.options, cmd="/bin/ping -c 1 www.google.com", startsecs=0,
                              dependent_startup_wait_for="ping2:running")

        self.setup_eventlistener()
        self.monitor_run_and_listen_until_no_more_events()


class DependentStartupWithGroupsTests(common.DependentStartupWithoutEventListenerTestsBase):

    def setUp(self):
        super(DependentStartupWithGroupsTests, self).setUp()
        # May not be needed
        os.environ['SUPERVISOR_SERVER_URL'] = "unix:///var/tmp/supervisor.sock"

    def test_basic_config(self):
        self.write_supervisord_config()
        consul_priority = 5
        slurmd_priority = 10
        slurmd3_priority = 15
        self.add_test_service('consul', self.options, priority=consul_priority)

        self.add_test_service('slurmd', self.options,
                              priority=slurmd_priority,
                              dependent_startup_wait_for="consul:running")
        self.add_test_service('slurmd2', self.options,
                              dependent_startup_inherit_priority=True,
                              dependent_startup_wait_for="consul:running")
        self.add_test_service('slurmd3', self.options,
                              priority=slurmd3_priority,
                              dependent_startup_inherit_priority=True,
                              dependent_startup_wait_for="consul:running slurmd:running")

        self.setup_eventlistener()
        self.monitor_run_and_listen_until_no_more_events()

        slurmd_service = self.monitor.services_handler._services['slurmd']
        self.assertEqual('slurmd', slurmd_service.group)
        self.assertEqual('slurmd', slurmd_service.group_and_procname)

        expected_slurm_procs = ['slurmd']
        self.assertEqual(expected_slurm_procs, list(slurmd_service.procs_state.keys()))

        self.assertEqual(slurmd_priority, slurmd_service.priority)
        self.assertEqual(slurmd_priority, slurmd_service.priority_effective)

        self.assertTrue(slurmd_service.dependent_startup)

        slurmd2_service = self.monitor.services_handler._services['slurmd2']
        self.assertEqual(True, slurmd2_service.options.inherit_priority)
        self.assertEqual(None, slurmd2_service.priority)
        self.assertEqual(consul_priority, slurmd2_service.priority_effective)

        slurmd3_service = self.monitor.services_handler._services['slurmd3']
        self.assertEqual(True, slurmd3_service.options.inherit_priority)
        self.assertEqual(slurmd3_priority, slurmd3_service.priority)
        self.assertEqual(consul_priority, slurmd3_service.priority_effective)

        expected_procs = ['consul', 'slurmd2', 'slurmd', 'slurmd3']
        self.assertEqual(expected_procs, self.processes_started)
        self.assertStateProcsRunning(expected_procs)

    def test_config_with_numprocs(self):
        self.write_supervisord_config()
        self.add_test_service('consul', self.options)
        self.add_test_service('slurmd', self.options,
                              dependent_startup_wait_for="consul:running",
                              numprocs=2,
                              process_name="%(program_name)s_%(process_num)02d")

        self.setup_eventlistener()
        self.monitor_run_and_listen_until_no_more_events()
        # self.print_procs()
        slurmd_service = self.monitor.services_handler._services['slurmd']
        self.assertEqual('slurmd', slurmd_service.name)
        self.assertEqual('slurmd', slurmd_service.group)
        self.assertEqual('slurmd', slurmd_service.group_and_procname)

        expected_slurm_procs = ['slurmd_00', 'slurmd_01']
        self.assertEqual(expected_slurm_procs, sorted(slurmd_service.procs_state.keys()))

        expected_procs = ['consul', 'slurmd:slurmd_00', 'slurmd:slurmd_01']
        self.assertEqual(expected_procs, sorted(self.processes_started))
        self.assertStateProcsRunning(expected_procs)

    def test_config_with_custom_process_name(self):
        self.write_supervisord_config()
        self.add_test_service('consul', self.options)
        self.add_test_service('slurmd', self.options,
                              dependent_startup_wait_for="consul:running",
                              process_name="slurmd_custom_procname")

        self.setup_eventlistener()
        self.monitor_run_and_listen_until_no_more_events()
        # self.print_procs()
        slurmd_service = self.monitor.services_handler._services['slurmd']
        self.assertEqual('slurmd', slurmd_service.name)
        self.assertEqual('slurmd', slurmd_service.group)
        self.assertEqual('slurmd:slurmd_custom_procname', slurmd_service.group_and_procname)

        expected_slurm_procs = ['slurmd_custom_procname']
        self.assertEqual(expected_slurm_procs, sorted(slurmd_service.procs_state.keys()))

        expected_procs = ['consul', 'slurmd:slurmd_custom_procname']
        self.assertEqual(expected_procs, sorted(self.processes_started))
        self.assertStateProcsRunning(expected_procs)


def unit_test_name_func(testcase_func, param_num, param):
    field_conf = param.kwargs['field_conf']
    name = "{name}_field_{field}_env_{env_var}_{env_value}".format(name=testcase_func.__name__, **field_conf)
    name = parameterized.to_safe_name(name)
    return name


expansion_success_config_option_fields = [
    param(field_conf={'field': 'priority', 'env_var': 'PRIORITY', 'env_value': 1}),
    param(field_conf={'field': 'autostart', 'env_var': 'AUTOSTART', 'env_value': 'false'}),
    param(field_conf={'field': 'autostart', 'env_var': 'AUTOSTART', 'env_value': 'true'}),
    param(field_conf={'field': 'dependent_startup', 'env_var': 'DEPENDENT_STARTUP', 'env_value': 'true'}),
    param(field_conf={'field': 'dependent_startup', 'env_var': 'DEPENDENT_STARTUP', 'env_value': 'false'}),
    param(field_conf={'field': 'dependent_startup_inherit_priority',
                      'env_var': 'DEPENDENT_STARTUP_INHERIT_PRIORITY', 'env_value': 'false'}),
    param(field_conf={'field': 'dependent_startup_inherit_priority',
                      'env_var': 'DEPENDENT_STARTUP_INHERIT_PRIORITY', 'env_value': 'true'})]


class ConfigEnvVariablesExpansionSuccessTests(common.DependentStartupWithoutEventListenerTestsBase):

    def fix_incompatibility(self, service_args):
        if 'autostart' in service_args:
            value = config_utils.expand_string('autostart', service_args.get('autostart'))
            if config_utils.safe_boolean(value) is True:
                service_args['dependent_startup'] = 'false'
        else:
            service_args['autostart'] = 'false'

            if 'dependent_startup' in service_args:
                if config_utils.safe_boolean(service_args['dependent_startup']) is True:
                    service_args['autostart'] = 'false'
            else:
                service_args['dependent_startup'] = 'true'

    @parameterized.expand(expansion_success_config_option_fields, name_func=unit_test_name_func)
    @mock.patch.dict(os.environ, {}, clear=True)
    def test_with_env_var_expansion(self, field_conf):
        field = field_conf['field']
        env_var = field_conf['env_var']
        env_value = field_conf['env_value']

        self.write_supervisord_config()
        env_var_expansion = "ENV_%s" % env_var
        field_value = "%({})s".format(env_var_expansion)
        os.environ[env_var] = str(env_value)
        service_args = {field: field_value}

        self.fix_incompatibility(service_args)
        self.add_service_file("service_with_env_var", **service_args)

        expected_log_msg = ("Parsing config with the following expansions: {'%s': '%s'}" %
                            (env_var_expansion, env_value))

        with LogCapturePrintable() as log_capture:
            self.setup_eventlistener()
            self.assertLogContains(
                log_capture,
                (plugin_logger_name, 'DEBUG', expected_log_msg))

        service = self.monitor.services_handler._services['service_with_env_var']

        # When parsing fails, expect default value
        opts_attr = field.replace('dependent_startup_', '')
        value_type_func = ServiceOptions.option_field_type_funcs[field]
        self.assertEqual(value_type_func(env_value), getattr(service.options, opts_attr))


expansion_success_config_options_wait_on = [
    param(field_conf={'field': 'dependent_startup_wait_for',
                      'env_var': 'DEPENDENT_STARTUP_WAIT_FOR', 'env_value': 'service-parent:running',
                      'parent_service': "service-parent"})]


class ConfigWaitForEnvVariablesExpansionSuccessTests(common.DependentStartupWithoutEventListenerTestsBase):

    def fix_incompatibility(self, service_args):
        if 'autostart' in service_args:
            value = config_utils.expand_string('autostart', service_args.get('autostart'))
            if config_utils.safe_boolean(value) is True:
                service_args['dependent_startup'] = 'false'
        else:
            service_args['autostart'] = 'false'

            if 'dependent_startup' in service_args:
                if config_utils.safe_boolean(service_args['dependent_startup']) is True:
                    service_args['autostart'] = 'false'
            else:
                service_args['dependent_startup'] = 'true'

    @parameterized.expand(expansion_success_config_options_wait_on, name_func=unit_test_name_func)
    @mock.patch.dict(os.environ, {}, clear=True)
    def test_wait_for_with_env_var_expansion(self, field_conf):
        field = field_conf['field']
        env_var = field_conf['env_var']
        env_value = field_conf['env_value']

        self.write_supervisord_config()
        env_var_expansion = "ENV_%s" % env_var
        field_value = "%({})s".format(env_var_expansion)
        os.environ[env_var] = str(env_value)

        parent_service = field_conf['parent_service']
        self.add_service_file(parent_service, autostart='false')

        service_args = {field: field_value, 'autostart': 'false'}
        self.fix_incompatibility(service_args)
        self.add_service_file("service-child", **service_args)
        self.setup_eventlistener()

        service = self.monitor.services_handler._services["service-child"]
        self.assertTrue(parent_service in service.options.wait_for_services)


expansion_fail_config_option_fields = [
    ('priority', 'ENV_PRIORITY', 'group_name, here, host_node_name, program_name'),
    ('autostart', 'ENV_AUTOSTART', 'group_name, here, host_node_name, program_name'),
    ('dependent_startup', 'ENV_DEPENDENT_STARTUP', 'group_name, here, host_node_name, program_name'),
    ('dependent_startup_inherit_priority', 'ENV_DEPENDENT_STARTUP_INHERIT_PRIORITY',
     'group_name, here, host_node_name, program_name'),
    ('dependent_startup_wait_for', 'ENV_DEPENDENT_STARTUP_WAIT_FOR', 'here')]


@mock.patch.dict(os.environ, {'ONLY_VAR': ''}, clear=True)
class ConfigEnvVariablesExpansionFailTests(common.DependentStartupWithoutEventListenerTestsBase):

    @parameterized.expand(expansion_fail_config_option_fields)
    def test_with_no_env_var_available(self, field, env_var, available_names):
        self.write_supervisord_config()
        field_value = "%({})s".format(env_var)
        service_name = 'service_with_env_var'
        self.add_service_file(service_name,
                              **{'dependent_startup': 'false', field: field_value})

        expected_log_msg = ("Error when parsing section "
                            "'program:{service_name}' field: {field}: "
                            "Format string '{field_value}' for 'program:service_with_env_var.{field}' "
                            "contains names ('{env_var}') "
                            "which cannot be expanded. Available names: ENV_ONLY_VAR, {available_names}")

        expected_log_msg = expected_log_msg.format(service_name=service_name,
                                                   field=field, env_var=env_var,
                                                   field_value=field_value,
                                                   available_names=available_names)

        with LogCapturePrintable() as log_capture:
            self.setup_eventlistener()
            self.assertLogContains(
                log_capture,
                (plugin_logger_name, 'WARNING', expected_log_msg))

        service = self.monitor.services_handler._services[service_name]

        # When parsing fails, expect default value
        opts_attr = field.replace('dependent_startup_', '')
        default_options = ServiceOptions(service_name, service_name)
        self.assertEqual(getattr(default_options, opts_attr), getattr(service.options, opts_attr))


@mock.patch.dict(os.environ, {'ONLY_VAR': ''}, clear=True)
class ConfigLoadFailureTests(common.DependentStartupWithoutEventListenerTestsBase):

    def test_that_loading_config_with_both_autostart_and_dependent_startup_true_fails(self):
        self.write_supervisord_config()
        self.add_service_file("service_with_env_var",
                              **{'dependent_startup': 'true', 'autostart': 'true'})
        self.mock_args.error_action = 'exit'
        self.setup_eventlistener(load_config=False)
        with self.assertRaises(DependentStartupError):
            self.monitor.load_config()

    def test_that_loading_config_with_both_autostart_and_dependent_startup_true_fails_with_log(self):
        self.write_supervisord_config()
        service_name = "service_with_env_var"
        service_conf, rendered = self.add_service_file(service_name,
                                                       **{'dependent_startup': 'true', 'autostart': 'true'})
        expected_log_msg = ("Error when reading config '{service_conf}': Service '{service_name}' "
                            "config has dependent_startup set to True, which requires autostart "
                            "to be set explicitly to false. autostart is currently True")
        expected_log_msg = expected_log_msg.format(service_conf=service_conf, service_name=service_name)

        with LogCapturePrintable() as log_capture:
            self.setup_eventlistener(load_config=False)
            self.monitor.load_config()
            self.assertLogContains(
                log_capture,
                (plugin_logger_name, 'WARNING', expected_log_msg))

    def test_that_loading_config_with_uknown_dependency_fails(self):
        self.write_supervisord_config()
        self.add_service_file("service",
                              **{'dependent_startup': 'true', 'autostart': 'false',
                                 'dependent_startup_wait_for': "uknown-service:running"})
        self.mock_args.error_action = 'exit'
        self.setup_eventlistener(load_config=False)
        with self.assertRaises(DependentStartupError):
            self.monitor.load_config()

    def test_that_loading_config_with_uknown_dependency_fails_with_log(self):
        self.write_supervisord_config()
        service_name = "service_with_env_var"
        service_dep = "uknown-service"
        self.add_service_file(service_name,
                              **{'dependent_startup': 'true', 'autostart': 'false',
                                 'dependent_startup_wait_for': "%s:running" % service_dep})

        expected_log_msg = "Service 'service_with_env_var' depends on unknown service '%s'" % service_dep
        with LogCapturePrintable() as log_capture:
            self.setup_eventlistener(load_config=False)
            self.monitor.load_config()
            self.assertLogContains(
                log_capture,
                (plugin_logger_name, 'WARNING', expected_log_msg))

        service = self.monitor.services_handler._services[service_name]
        self.assertEqual(0, len(service.options.wait_for_services))


class DependentStartupEventParseTests(common.DependentStartupWithoutEventListenerTestsBase):

    def setUp(self):
        super(DependentStartupEventParseTests, self).setUp()
        # Not be needed
        os.environ['SUPERVISOR_SERVER_URL'] = "unix:///var/tmp/supervisor.sock"
        test_instance = self

        class StdinEvents(StdinManualEventsWrapper):

            def get_process_event_line(self, index):
                line = test_instance.eventline_fmt % self.events[index]
                return line

        self.stdin_wrapper = StdinEvents()

    def test_event_data_multiline(self):
        self.eventline_fmt = ("processname:%(processname)s groupname:%(groupname)s "
                              "from_state:%(from_state)s pid:%(pid)s "
                              "channel:stderr\n: No such file or directory")
        self.write_supervisord_config()
        self.add_test_service('consul', self.options)
        self.setup_eventlistener()
        self.monitor_run_and_listen_until_no_more_events()

    def test_event_data_singe_line(self):
        self.eventline_fmt = ("processname:%(processname)s groupname:%(groupname)s "
                              "from_state:%(from_state)s pid:%(pid)s")
        self.write_supervisord_config()
        self.add_test_service('consul', self.options)
        self.setup_eventlistener()
        self.monitor_run_and_listen_until_no_more_events()
