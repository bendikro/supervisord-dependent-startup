#!/usr/bin/env python
#
# An event listener for supervisord that handles ordered startup of services
#
from __future__ import print_function

import argparse
from collections import OrderedDict
import glob
import logging
import os
import platform
import socket
import sys
import traceback

# isort:imports-thirdparty
from supervisor import childutils, states
from supervisor.datatypes import boolean, integer
from supervisor.options import UnhosedConfigParser
from supervisor.states import RUNNING_STATES
from supervisor.xmlrpc import Faults, xmlrpclib
import toposort


log_str_to_levels = {'critial': logging.CRITICAL,
                     'error': logging.ERROR,
                     'warning': logging.WARNING,
                     'warn': logging.WARNING,
                     'info': logging.INFO,
                     'debug': logging.DEBUG,
                     'notset': logging.NOTSET}


def get_log_level_from_string(str_level, default='info'):
    default_level = log_str_to_levels.get(default, logging.INFO)
    return log_str_to_levels.get(str_level.lower(), default_level)


def get_str_from_log_level(level):
    if level == logging.NOTSET:
        return "unset"
    for k, v in log_str_to_levels.items():
        if level == v:
            return k
    raise Exception("No level with value %s" % level)


plugin_logger_name = 'supervisord_dependent_startup'

log = logging.getLogger(plugin_logger_name)


class SupervisorProcessStates(states.ProcessStates):

    def __init__(self):
        self._states = None

    def __contains__(self, state):
        if self._states is None:
            self._set_process_states()
        return state in self._states

    def is_running(self, state):
        if self._states is None:
            self._set_process_states()
        # RUNNING_STATES contains RUNNING, STARTING, BACKOFF
        return self._states[state] in RUNNING_STATES

    def _set_process_states(self):
        states = {}
        for state in dir(self.__class__):
            if state.startswith("_") or callable(getattr(self.__class__, state)):
                continue
            val = getattr(self.__class__, state)
            if type(val) != int:
                log.warning("Process state value for '%s' is not an int but '%s': %s" % (state, type(val), val))
            states[state] = val
        self._states = states

    def process_state_event_to_string(self, event_name):
        return event_name.replace('PROCESS_STATE_', '')


process_states = SupervisorProcessStates()


def get_all_configs(root_path):
    """Get all the configuration files to be parsed.  This is kinda weird because in
    order to do it we must parse the configs.
    """
    retval = [root_path]
    parser = UnhosedConfigParser()
    parser.read(root_path)
    if 'include' in parser.sections() and parser.has_option('include', 'files'):
        files = parser.get('include', 'files').split()
        base_dir = os.path.dirname(os.path.abspath(root_path))
        for pattern in files:
            if pattern.startswith('/'):
                for config in glob.glob(pattern):
                    retval.extend(get_all_configs(config))
            else:
                for config in glob.glob(os.path.join(base_dir, pattern)):
                    retval.extend(get_all_configs(config))
    return retval


def get_config_search_paths():
    script_path = os.path.abspath(os.path.dirname(os.path.dirname(sys.argv[0])))
    cwd = os.getcwd()
    search_paths = [script_path + "/",
                    os.path.join(script_path, 'etc/'),
                    cwd + "/",
                    os.path.join(cwd, 'etc/'),
                    '/etc/',
                    '/etc/supervisor/']
    return search_paths


def search_for_config_file(paths, config_filename):
    for path in paths:
        conf_file = os.path.join(path, config_filename)
        if os.path.exists(conf_file):
            return conf_file
    return None


class DependentStartupError(Exception):
    pass


class ConfigParser(UnhosedConfigParser):

    def __init__(self, *args, **kwargs):
        self.common_expansions = kwargs.pop('common_expansions', None)
        UnhosedConfigParser.__init__(self, *args, **kwargs)

    def safeget(self, section, option, default=None, type_func=None, **kwargs):
        """
        Safely get a config value without raising ValueError

        Args:
            section(str): The section to get the value from
            option(str): The option name
            default(any): The value to return if getting the option value fails
            type_func(func): Function call on the value to convert to proper type
        """
        try:
            expansions = kwargs.pop('expansions', {})
            expansions.update(self.common_expansions)
            value = self.saneget(section, option, default=default, expansions=expansions, **kwargs)
            if type_func is not None:
                value = type_func(value)
            return value
        except ValueError as err:
            log.warning("Error when parsing section '%s' field: %s: %s", section, option, err)
            return default


class ServiceOptions(object):

    valid_wait_on_states = ['STARTING', 'RUNNING', 'BACKOFF', 'STOPPING', 'EXITED', 'FATAL']
    wait_for_opts_string = 'dependent_startup_wait_for'
    inherit_priority_opts_string = 'dependent_startup_inherit_priority'
    option_field_type_funcs = {
        'priority': integer,
        'autostart': boolean,
        'dependent_startup': boolean,
    }

    def __init__(self, program_name, group_name):
        self.program_name = program_name
        self.group_name = group_name
        self.procnames = []
        self.opts = {}
        self.wait_for_services = OrderedDict()
        ServiceOptions.option_field_type_funcs[self.inherit_priority_opts_string] = boolean

    def parse(self, parser, section_name):
        """
        Args:
            parser(UnhosedConfigParser): the config parser object
            section_name(str): The name of the section to get the options from

        """
        def get_option(option, expansions={}, **kwargs):
            if 'type_func' not in kwargs:
                kwargs['type_func'] = self.option_field_type_funcs.get(option)

            expansions.update({'program_name': self.program_name, 'group_name': self.group_name,
                               'host_node_name': platform.node()})
            return parser.safeget(section_name, option, expansions=expansions, **kwargs)

        def set_option(option, expansions={}, **kwargs):
            option_value = get_option(option, expansions=expansions, **kwargs)
            if option_value is not None:
                self.opts[option] = option_value

        if parser.has_option(section_name, 'priority'):
            set_option('priority')
        if parser.has_option(section_name, 'autostart'):
            set_option('autostart')
        if parser.has_option(section_name, 'dependent_startup'):
            set_option('dependent_startup')
        if parser.has_option(section_name, 'numprocs'):
            set_option('numprocs')
        if parser.has_option(section_name, self.inherit_priority_opts_string):
            set_option(self.inherit_priority_opts_string)
        if parser.has_option(section_name, self.wait_for_opts_string):
            wait_for = parser.safeget(section_name, self.wait_for_opts_string)
            if wait_for is not None:
                self._parse_wait_for(section_name, wait_for)

        if parser.has_option(section_name, 'process_name'):
            numprocs = int(self.opts.get('numprocs', 0))
            expansions = {}
            if numprocs:
                numprocs_start = int(parser.safeget(section_name, 'numprocs_start', default=0))

                for process_num in range(numprocs_start, numprocs + numprocs_start):
                    expansions = {'process_num': process_num}
                    process_name = get_option('process_name', expansions=expansions)
                    self.procnames.append(process_name)
            else:
                process_name = get_option('process_name', expansions=expansions)
                self.procnames.append(process_name)
        else:
            self.procnames.append(self.program_name)

    def _parse_wait_for(self, section_name, wait_for):
        for dep in wait_for.split(' '):
            # By default, depend on the process being in RUNNING state
            dep_states = ["RUNNING"]
            depsplit = dep.split(':')
            dep_service = depsplit[0]
            if len(depsplit) == 2:
                dep_states = [state.upper() for state in depsplit[1].split(',')]
                for state in list(dep_states):
                    if state not in self.valid_wait_on_states:
                        log.warning("Ignoring invalid state '%s' in '%s' for '%s'" %
                                    (state, self.wait_for_opts_string, section_name))
                        dep_states.remove(state)
            self.wait_for_services[dep_service] = dep_states

    @property
    def autostart(self):
        return self.opts.get('autostart', True)

    @property
    def priority(self):
        return self.opts.get('priority', None)

    @property
    def dependent_startup(self):
        return self.opts.get('dependent_startup', False)

    @property
    def numprocs(self):
        return self.opts.get('numprocs', None)

    @property
    def inherit_priority(self):
        return self.opts.get(self.inherit_priority_opts_string, False)

    @property
    def wait_for(self):
        if self.wait_for_services:
            return " ".join(["%s:%s" % (dep, ",".join(state for state in self.wait_for_services[dep]))
                             for dep in self.wait_for_services])
        else:
            return None

    def wait_for_state(self, dep):
        return ",".join(state for state in self.wait_for_services[dep])

    def __str__(self):
        attrs = []
        for attr in ['dependent_startup', 'autostart', self.inherit_priority_opts_string]:
            if attr in self.opts:
                attrs.append("%s: %s" % (attr, self.opts[attr]))

        if self.wait_for_services:
            attrs.append("%s: %s" % (self.wait_for_opts_string, str(self.wait_for_services)))

        ret = ", ".join(attrs)
        return ret


class Service(object):

    # The default priority used by supervisor when no priority is set
    default_priority_sort = 999

    def __init__(self, services_handler):
        self.name = None
        self.group = None
        self.services_handler = services_handler
        self.options = None
        self.states_reached = []
        self.procs_state = {}

    def has_process(self, procname):
        """
        Args:
            procname (str): Process name

        Returns:
            bool: True of service has this process name, else False

        """
        return procname in self.procs_state

    def parse_section(self, parser, section_name, procs_to_group):
        """
        Args:
            parser(UnhosedConfigParser): the config parser object
            section_name(str): The name of the section to get the options from

        """
        self.name = section_name.split(':', 1)[1]
        self.group = procs_to_group.get(self.name, self.name)
        self.options = ServiceOptions(self.name, self.group)
        self.options.parse(parser, section_name)

        self.procs_state.update({procname: {'states_reached': []} for procname in self.options.procnames})

        if self.options.dependent_startup:
            error_msg = None

            if self.options.autostart:
                error_msg = ("Service '%s' config has dependent_startup set to %s, "
                             "which requires autostart to be set explicitly to false. "
                             "autostart is currently %s" %
                             (self.name, self.options.dependent_startup,
                              self.options.opts.get('autostart', 'not set')))
                log.warning("Error when reading config '%s': %s" %
                            (parser.section_to_file[section_name], error_msg))

            if error_msg:
                if self.services_handler.args.error_action == 'exit':
                    raise DependentStartupError(error_msg)
                elif self.services_handler.args.error_action in ['skip', 'ignore']:
                    log.warning("Disable handling service '%s'" % (self.name))
                    self.options.opts['dependent_startup'] = False

    def has_reached_states(self, states):
        """
        Args:
            states (list): List of states

        Returns:
            bool: True of one of the states have been reached, else False

        """
        for state in states:
            all_procs_reached_state = True
            for procname in self.procs_state:
                if state not in self.procs_state[procname]['states_reached']:
                    all_procs_reached_state = False
                    break
            if all_procs_reached_state:
                return True
        return False

    def process_state_update(self, procname, state):
        """
        Add state to the process list of states it has reached
        """
        self.procs_state[procname]['states_reached'].append(state)

    @property
    def procname(self):
        """
        Get the process name for this service.
        Value is invalid when service has multiple processes
        """
        return "%s" % (list(self.procs_state.keys())[0])

    @property
    def group_and_procname(self):
        """
        Get the name used to start the process or process group

        If numprocs > 1, the processes are named prefixed with the service name as the group name

        """
        if len(self.procs_state) > 1:
            if self.name == self.group:
                # Process not part of a [group:x] section
                # It is an error to use this value if numprocs is > 0
                return self.name
            else:
                return "%s:%s" % (self.group, self.name)
        else:
            procname = self.procname
            if self.name != self.group:
                # Process is part of a [group:x] section
                return "%s:%s" % (self.group, procname)

            if procname != self.name:
                # With custom process_name the group must be prefixed
                return "%s:%s" % (self.group, procname)

            return "%s" % (self.name)

    @property
    def dependent_startup(self):
        """bool: If this service is handled by this plugin."""
        return self.options.dependent_startup

    @property
    def priority_sort(self):
        """int: A sortable service priority. If unset, return default_priority_sort"""
        priority = self.default_priority_sort
        if self.priority:
            priority = self.priority

        if self.options.inherit_priority:
            for dep in self.options.wait_for_services:
                priority = min(priority, self.services_handler._services[dep].priority_sort)
        return priority

    @property
    def priority_effective(self):
        """int: The service priority. None if unset"""
        priority = self.priority_sort
        if priority == self.default_priority_sort:
            return None
        return priority

    @property
    def priority(self):
        """int: if priority i set, else None"""
        return self.options.priority

    def depends_on_diff(self, other):
        """
        Return set of dependencies in self that other does not have
        """
        return set(self.options.wait_for_services.keys()).difference(
            set(other.options.wait_for_services.keys()))

    def __str__(self):
        return "Service(name=%s, group=%s, %s)" % (self.name, self.group, str(self.options))

    def __repr__(self):
        return self.__str__()


class ProcessHandler(object):

    states_done = ['RUNNING']

    def __init__(self, rpc):
        self.rpc = rpc
        self.proc_info = OrderedDict()
        self.proc_by_group = {}

    def update_group_procs(self):
        config_info = self.rpc.supervisor.getAllConfigInfo()
        for c_info in config_info:
            g_name = c_info['group']
            if g_name not in self.proc_by_group:
                self.proc_by_group[g_name] = []
            self.proc_by_group[g_name].append(c_info['name'])

    def update_proc_info(self, service=None):
        if service:
            info = [self.rpc.supervisor.getProcessInfo(procname)
                    for procname in self.get_procs(service, with_group=True)]
        else:
            info = self.rpc.supervisor.getAllProcessInfo()

        for p_info in info:
            self.proc_info[p_info['name']] = p_info

    def get_service_states(self, service, update=False):
        if update:
            self.update_proc_info(service=service)
        procs = self.get_procs(service)
        return [(self.proc_info[procname]['name'], self.proc_info[procname]['statename']) for procname in procs]

    def is_startable(self, service):
        states = self.get_service_states(service)
        startable = [process_states.is_running(state) or state in ['FATAL'] for sname, state in states]
        return True not in startable

    def get_procs(self, service, with_group=False):
        """
        Args:
            service (Service): Service object

        Returns:
            list(str): List of process names

        """
        if with_group:
            return ["%s:%s" % (service.group, procname) for procname in service.procs_state]
        else:
            return [procname for procname in service.procs_state]

    def start_service(self, service, wait=True):
        sname = service.name
        state_str = self.get_service_state_str(service)[0]

        log.info("Starting service: {} (State: {})".format(sname, state_str))

        if not self.is_startable(service):
            log.info("Service: %s has state: %s. Will not attempt to start service" % (sname, state_str))
            return False

        try:
            group_and_procname = service.group_and_procname
            if service.options.numprocs is not None and int(service.options.numprocs) > 1:
                return self.rpc.supervisor.startProcessGroup(group_and_procname, wait)
            else:
                return self.rpc.supervisor.startProcess(group_and_procname, wait)
        except xmlrpclib.Fault as err:
            log.warning("Error when starting service '%s' (group: %s): %s" % (sname, service.group, err))
            return False

    def get_service_state_str(self, service, compact=True):
        states = self.get_service_states(service, update=True)
        if len(states) > 1:
            states = ["%s: %-8s" % (name, state) for name, state in states]
            if compact:
                return [", ".join(states).strip()]
            else:
                return states
        else:
            return [states[0][1]]


class ServicesHandler(ProcessHandler):
    """ServicesHandler keep track of all the services managed by supervisor
    """

    def __init__(self, rpc, args):
        super(ServicesHandler, self).__init__(rpc)
        self.max_name_len = 0
        self.indent = 0
        self.args = args
        self._services = OrderedDict()

    def parse_config(self, parser):
        environ_expansions = {}
        for k, v in os.environ.items():
            environ_expansions['ENV_%s' % k] = v
        parser.expansions = environ_expansions
        log.debug("Parsing config with the following expansions: %s", environ_expansions)

        procs_to_group = {}
        # Get process groups for services specified in [group:x] sections
        for section_name in parser.sections():
            if section_name.startswith('group:'):
                programs = parser.safeget(section_name, "programs")
                groupname = section_name.split(':', 1)[1]
                for program in programs.split(','):
                    procs_to_group[program] = groupname

        for section_name in parser.sections():
            if section_name.startswith('program:'):
                service = Service(self)
                service.parse_section(parser, section_name, procs_to_group)
                self._services[service.name] = service

        self.verify_dependencies()
        ordered = self.get_sorted_services_list()
        ordered_services = OrderedDict([(s_name, self._services[s_name]) for s_name in ordered])
        self._services = ordered_services

    def verify_dependencies(self):
        for sname, v in self._services.items():
            deps = set(v.options.wait_for_services.keys())
            for dep in deps:
                if dep not in self._services:
                    msg = "Service '%s' depends on unknown service '%s'" % (sname, dep)
                    log.warning(msg)
                    if self.args.error_action == 'exit':
                        raise DependentStartupError(msg)
                    else:
                        # Must remove the dependency
                        log.warning("Removing dependency '%s' from service %s", dep, sname)
                        del v.options.wait_for_services[dep]

    def get_sorted_services_list(self):
        deps_dict = {}
        for k, v in self._services.items():
            deps_dict[k] = set(v.options.wait_for_services.keys())

        try:
            result = []
            # Iterator returns a set with services on the same level
            for d in toposort.toposort(deps_dict):
                # First sort by priority, then name
                ordered = sorted(d, key=lambda service:
                                 (self._services[service].priority_sort, service), reverse=False)
                result.extend(ordered)
            return result
        except toposort.CircularDependencyError as err:
            log.error("Cirular dependencies detected: %s" % (err))
            raise DependentStartupError(err)

    def service_wait_for_satisifed(self, service):
        for dep_sname in service.options.wait_for_services:
            dep_service = self._services[dep_sname]
            satisifed = False
            for required_state in service.options.wait_for_services[dep_sname]:
                if dep_service.has_reached_states([required_state]):
                    satisifed = True

            if not satisifed:
                dep_states = self.get_service_states(dep_service)
                log.debug("Service '%s' depends on '%s' to reach state %s. "
                          "Process state for service '%s' is currently %s" %
                          (service.name, dep_sname, service.options.wait_for_state(dep_sname),
                           dep_sname, dep_states))
                return False
        return True

    @property
    def services(self):
        return self._services.values()

    def get_service(self, name):
        return self._services.get(name)

    def is_service_done(self, service):
        return service.has_reached_states(['RUNNING', 'FATAL'])

    def update_sevices_info(self):
        super(ServicesHandler, self).update_proc_info()
        self.max_name_len = 0
        for k, p_info in self.proc_info.items():
            self.max_name_len = max(self.max_name_len, len(p_info['name']))

    def update_state_event(self, procname, state):
        """
        Update the service with the process state
        """
        for service in self._services.values():
            if service.has_process(procname):
                service.process_state_update(procname, state)

    def get_service_str(self, service):
        fmt_param = {'name': service.name, 'name_len': self.max_name_len,
                     'dependent_startup': str(service.options.dependent_startup)}
        fmt = "{name:{name_len}}  {state:<30}  dependent_startup: {dependent_startup:5}"

        if service.options.wait_for_services:
            fmt += "  wait_for: '{wait_for}'"
            fmt_param['wait_for'] = service.options.wait_for

        effective = service.priority_effective
        if effective is not None:
            fmt += "  priority: {priority:4}"
            fmt_param['priority'] = effective

            if service.priority is None:
                fmt += " (inherited)"

        for state_str in self.get_service_state_str(service, compact=False):
            _param = {'state': state_str}
            _param.update(**fmt_param)
            yield fmt.format(**_param)
        return

    def log_services_list(self):
        for service in self.services:
            for service_str in self.get_service_str(service):
                log.info(" - %s" % service_str)

    def update_config_groups(self):
        self.update_group_procs()
        for group, service_names in self.proc_by_group.items():
            log.debug("Updating process group (%s): %s" % (group, service_names))
            for sname in service_names:
                if sname in self._services:
                    self._services[sname].group = group

        for p_info, v in self.proc_info.items():
            log.info("Proc(%s): %s" % (p_info, v))


class DependentStartup(object):

    process_state_events = ['PROCESS_STATE']

    def __init__(self, args, config_file, **kwargs):
        self.config_file = config_file
        self.interval = kwargs.get('interval', 1.0)
        self.stdin = kwargs.get('stdin', sys.stdin)
        self.stdout = kwargs.get('stdout', sys.stdout)
        self.stderr = kwargs.get('stderr', sys.stderr)
        self.rpc = kwargs.get('rpcinterface', None)
        self.expansions = {
            'here': os.path.abspath(os.path.dirname(config_file))
        }
        if not self.rpc:
            self.rpc = childutils.getRPCInterface(os.environ)
            try:
                api_version = self.rpc.supervisor.getAPIVersion()
                log.info("Connected to supervisor with API version: %s", api_version)
            except socket.error:
                raise DependentStartupError("Failed to connect to supervisord:\n%s" % traceback.format_exc())

        self.startup_done = False
        self.start_first = None
        self.plugin_initialized = False
        self.services_handler = ServicesHandler(self.rpc, args)

        log.debug("Args: %s" % args)
        if kwargs.get('load_config', True) is True:
            self.load_config()

    def load_config(self):
        log.info("Reading supervisor config: %s" % self.config_file)
        parser = ConfigParser(common_expansions=self.expansions)
        parser.read(get_all_configs(self.config_file))
        self.services_handler.parse_config(parser)

    def write_stderr(self, msg):
        self.stderr.write(msg)
        self.stderr.flush()

    def get_event(self, headers, payload, short=True):
        payload_headers = self.parse_event_headers(payload)
        payload_headers.update(headers)
        if short:
            new_state = process_states.process_state_event_to_string(payload_headers['eventname'])
            return payload_headers, ("Service %s went from %s to %s" %
                                     (payload_headers['processname'], payload_headers['from_state'], new_state))
        return payload_headers, "headers: %s, payload: %s" % (headers, payload)

    def parse_event_headers(self, payload):
        header_line = payload.split('\n', 1)[0]
        payload_headers = childutils.get_headers(header_line)
        return payload_headers

    def handle_event(self, headers, payload):
        if self.startup_done:
            return

        event_parsed, event_str = self.get_event(headers, payload)
        log.info("")
        log.info("New event: %s" % event_str)

        if headers['eventname'].startswith('PROCESS_STATE') and not self.startup_done:
            payload_headers = self.parse_event_headers(payload)
            state = process_states.process_state_event_to_string(headers['eventname'])
            event_process = payload_headers['processname']
            log.debug("Event from service '%s' (%s)" % (event_process, state))
            log.debug("headers = {}".format(repr(headers)))
            log.debug("payload = {}".format(repr(payload_headers)))

            self.services_handler.update_state_event(event_process, state)
            self.services_handler.update_proc_info()

            if self.start_first:
                log.info("Starting immediately: %s" % self.start_first)
                self.services_handler.start_service(self.start_first, wait=False)
                log.info("Starting ordered services")
                self.services_handler.update_proc_info(service=self.start_first)
                self.start_first = None

            self.start_services()
        return event_parsed

    def start_services(self):
        log.info("Services:")
        self.services_handler.log_services_list()

        # All services we should be handling that aren't already done
        service_to_be_started = []
        for service in self.services_handler.services:
            if not service.dependent_startup:
                continue

            if self.services_handler.is_service_done(service):
                state_str = self.services_handler.get_service_state_str(service)[0]
                log.debug("Ignoring service '%s' with state: %s" % (service.name, state_str))
                continue

            service_to_be_started.append(service)

        # All services with dependent_startup enabled are done
        if not service_to_be_started:
            self.startup_done = True
            log.info("No more processes to start for initial startup, ignoring all future events.")
            return

        log.info("Services not yet running (%s): %s",
                 len(service_to_be_started), ", ".join([s.name for s in service_to_be_started]))

        to_start_priorites = {}
        for service in service_to_be_started:
            if not self.services_handler.is_startable(service):
                continue

            if not self.services_handler.service_wait_for_satisifed(service):
                continue

            priority = service.priority_effective
            procs = to_start_priorites.get(priority, list())
            procs.append(service)
            to_start_priorites[priority] = procs

        if to_start_priorites:
            priority_start = sorted(to_start_priorites.keys(), key=lambda x: float('inf') if x is None else x)
            for priority in priority_start:
                for service in to_start_priorites[priority]:
                    self.services_handler.start_service(service, wait=False)

    def _listen(self):
        while not self.startup_done:
            headers, payload = childutils.listener.wait(self.stdin, self.stdout)
            event_parsed = self.handle_event(headers, payload)
            childutils.listener.ok(self.stdout)
            yield event_parsed

    def listen(self):
        for l in self._listen():
            pass

    def run_and_listen(self):
        log.info("")
        log.info("")
        self.run()
        self.listen()

    def run(self):
        self.services_handler.update_sevices_info()
        self.services_handler.update_config_groups()
        self.services_handler.log_services_list()

        for service in self.services_handler.services:
            if service.options.autostart or not service.dependent_startup:
                continue

            if not self.services_handler.is_startable(service):
                continue

            if not self.services_handler.service_wait_for_satisifed(service):
                continue

            self.start_first = service
            break

        if not self.start_first:
            self.startup_done = True
            log.info("Found no services to start")


default_log_format = "%(asctime)s - %(name)s - [%(levelname)-7s] %(message)s"


def main():
    search_paths = get_config_search_paths()
    parser = argparse.ArgumentParser()
    parser.add_argument('--log-file', help="Log to file instead of stderr")
    parser.add_argument('--log-level', choices=log_str_to_levels.keys(), default="info",
                        help="Log level. Default: %(default)s")
    parser.add_argument('--log-format', default=default_log_format,
                        help="Logging format. Default: %(default)s")
    parser.add_argument('-c', '--config', help="Full path to the supervisor config file. "
                        "If not provided, the config will be searched for in the following "
                        "paths: '%s'" % "'\n'".join(search_paths))
    parser.add_argument('--config-filename', default="supervisord.conf",
                        help="The name of the config file to search for "
                        "if the config is not provided. Default: %(default)s")
    parser.add_argument('--fail-on-warning', default=False, action='store_true',
                        help=argparse.SUPPRESS)
    parser.add_argument('--error-action', default='skip', choices=['exit', 'skip', 'ignore'],
                        help="The action to perform when encountering service config errors")
    args = parser.parse_args()

    log_level = get_log_level_from_string(args.log_level, default='info')

    if args.log_file:
        logging.basicConfig(filename=args.log_file, level=log_level, format=args.log_format)
    else:
        logging.basicConfig(stream=sys.stderr, level=log_level, format=args.log_format)

    if args.fail_on_warning:
        args.error_action = "exit"

    log.info("")
    log.info("supervisord-dependent-startup event listener starting...")

    config_file = None
    if args.config:
        config_file = args.config
    else:
        config_file = search_for_config_file(search_paths, args.config_filename)

    if config_file is None:
        log.warning("Unable to find a config file")
        return 4
    if not os.path.exists(config_file):
        log.warning("Config path {} does not exist!".format(config_file))
        return 2

    event_listener = DependentStartup(args, config_file)
    event_listener.run_and_listen()


def run():
    exit_code = 0
    try:
        main()
    except xmlrpclib.Fault as error:
        if error.faultCode != Faults.SHUTDOWN_STATE:
            log.error("Error occured:", exc_info=sys.exc_info())
            exit_code = 3
    except:  # noqa: E722
        log.error("Error occured:", exc_info=sys.exc_info())
        exit_code = 3
    sys.exit(exit_code)


if __name__ == '__main__':
    run()
