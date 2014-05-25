###############################################################################
##
##  Copyright (C) 2014 Tavendo GmbH
##
##  This program is free software: you can redistribute it and/or modify
##  it under the terms of the GNU Affero General Public License, version 3,
##  as published by the Free Software Foundation.
##
##  This program is distributed in the hope that it will be useful,
##  but WITHOUT ANY WARRANTY; without even the implied warranty of
##  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
##  GNU Affero General Public License for more details.
##
##  You should have received a copy of the GNU Affero General Public License
##  along with this program. If not, see <http://www.gnu.org/licenses/>.
##
###############################################################################

from __future__ import absolute_import

__all__ = ['NativeWorkerSession']


import os
import sys
import gc

from datetime import datetime

from twisted.python import log
from twisted.internet import reactor
from twisted.internet.defer import Deferred, \
                                   DeferredList, \
                                   inlineCallbacks, \
                                   returnValue

from twisted.internet.task import LoopingCall


try:
   ## Manhole support needs a couple of packages optional for Crossbar.
   ## So we catch import errors and note those.
   ##
   import Crypto # twisted.conch.manhole_ssh will import even without, but we _need_ SSH
   import pyasn1
   from twisted.cred import checkers, portal
   from twisted.conch.manhole import ColoredManhole
   from twisted.conch.manhole_ssh import ConchFactory, TerminalRealm
except ImportError as e:
   _HAS_MANHOLE = False
   _MANHOLE_MISSING_REASON = str(e)
else:
   _HAS_MANHOLE = True
   _MANHOLE_MISSING_REASON = None


try:
   import psutil
except ImportError:
   _HAS_PSUTIL = False
else:
   _HAS_PSUTIL = True
   from crossbar.common.processinfo import ProcessInfo, SystemInfo



from autobahn.util import utcnow, utcstr, rtime
from autobahn.twisted.wamp import ApplicationSession
from autobahn.wamp.exception import ApplicationError
from autobahn.wamp.types import PublishOptions, \
                                RegisterOptions

from crossbar import controller
from crossbar.common.reloader import TrackingModuleReloader
from crossbar.twisted.endpoint import create_listening_port_from_config


class ManholeService:
   """
   Manhole service running inside a (native) worker.

   This class is for _internal_ use within NativeWorkerSession.
   """

   def __init__(self, config, port):
      """
      Ctor.

      :param config: The configuration the manhole service was started with.
      :type config: dict
      :param port: The listening port this service runs on.
      :type port: instance of IListeningPort
      """
      self.started = datetime.utcnow()
      self.config = config
      self.port = port


   def marshal(self):
      """
      Marshal object information for use with WAMP calls/events.

      :returns: dict -- The marshalled information.
      """
      now = datetime.utcnow()
      return {
         'started': utcstr(self.started),
         'uptime': (now - self.started).total_seconds(),
         'config': self.config
      }



class NativeWorkerSession(ApplicationSession):
   """
   A native Crossbar.io worker process. The worker will be connected
   to the node's management router running inside the node controller
   via WAMP-over-stdio.
   """

   WORKER_TYPE = 'native'


   def onConnect(self):
      """
      Called when the worker has connected to the node's management router.
      """
      self.debug = self.config.extra.debug
      self.debug_app = True

      if self.debug:
         log.msg("Connected to management router")

      self._module_tracker = TrackingModuleReloader(debug = True)

      self._started = datetime.utcnow()

      self._manhole_service = None

      if _HAS_PSUTIL:
         self._pinfo = ProcessInfo()
         self._pinfo_monitor = None
         self._pinfo_monitor_seq = 0
      else:
         self._pinfo = None
         self._pinfo_monitor = None
         self._pinfo_monitor_seq = None
         log.msg("Warning: process utilities not available")

      self.join(self.config.realm)



   @inlineCallbacks
   def onJoin(self, details):
      """
      Called when worker process has joined the node's management realm.
      """
      procs = [
         'start_manhole',
         'stop_manhole',
         'get_manhole',
         'trigger_gc',
         'get_cpu_affinity',
         'set_cpu_affinity',
         'utcnow',
         'started',
         'uptime',
         'get_pythonpath',
         'add_pythonpath',
         'get_process_info',
         'get_process_stats',
         'set_process_stats_monitoring'
      ]

      dl = []
      for proc in procs:
         uri = 'crossbar.node.{}.worker.{}.{}'.format(self.config.extra.node, self.config.extra.worker, proc)
         if True or self.debug:
            log.msg("Registering procedure '{}'".format(uri))
         dl.append(self.register(getattr(self, proc), uri, options = RegisterOptions(details_arg = 'details', discloseCaller = True)))

      regs = yield DeferredList(dl)

      if self.debug:
         log.msg("NativeWorker registered {} procedures".format(len(regs)))

      ## signal that this worker is ready for setup. the actual setup procedure
      ## will either be sequenced from the local node configuration file or remotely
      ## from a management service
      ##
      pub = yield self.publish('crossbar.node.{}.on_worker_ready'.format(self.config.extra.node),
         {'type': self.WORKER_TYPE, 'id': self.config.extra.worker, 'pid': os.getpid()},
         options = PublishOptions(acknowledge = True))

      if self.debug:
         log.msg("NativeWorker ready event published")



   def get_process_info(self, details = None):
      """
      Get process information (open files, sockets, ...).

      :returns: dict -- Dictionary with process information.
      """
      if self.debug:
         log.msg("NativeWorkerSession.get_process_info")

      if self._pinfo:
         return self._pinfo.get_info()
      else:
         emsg = "ERROR: could not retrieve process statistics - required packages not installed"
         raise ApplicationError("crossbar.error.feature_unavailable", emsg)



   def get_process_stats(self, details = None):
      """
      Get process statistics (CPU, memory, I/O).

      :returns: dict -- Dictionary with process statistics.
      """
      if self.debug:
         log.msg("NativeWorkerSession.get_process_stats")

      if self._pinfo:
         return self._pinfo.get_stats()
      else:
         emsg = "ERROR: could not retrieve process statistics - required packages not installed"
         raise ApplicationError("crossbar.error.feature_unavailable", emsg)



   def set_process_stats_monitoring(self, interval, details = None):
      """
      Enable/disable periodic publication of process statistics.

      :param interval: The monitoring interval in seconds. Set to 0 to disable monitoring.
      :type interval: float
      """
      if self.debug:
         log.msg("NativeWorkerSession.set_process_stats_monitoring", interval)

      if self._pinfo:

         stats_monitor_set_topic = 'crossbar.node.{}.worker.{}.on_process_stat_monitoring_set'.format(self.config.extra.node, self.config.extra.worker)

         ## stop and remove any existing monitor
         if self._pinfo_monitor:
            self._pinfo_monitor.stop()
            self._pinfo_monitor = None

            self.publish(stats_monitor_set_topic, 0, options = PublishOptions(exclude = [details.caller]))

         ## possibly start a new monitor
         if interval > 0:
            stats_topic = 'crossbar.node.{}.worker.{}.on_process_stats'.format(self.config.extra.node, self.config.extra.worker)

            def publish_stats():
               stats = self._pinfo.get_stats()
               self._pinfo_monitor_seq += 1
               stats['seq'] = self._pinfo_monitor_seq
               self.publish(stats_topic, stats)

            self._pinfo_monitor = LoopingCall(publish_stats)
            self._pinfo_monitor.start(interval)

            self.publish(stats_monitor_set_topic, interval, options = PublishOptions(exclude = [details.caller]))
      else:
         emsg = "ERROR: cannot setup process statistics monitor - required packages not installed"
         raise ApplicationError("crossbar.error.feature_unavailable", emsg)



   def trigger_gc(self, details = None):
      """
      Triggers a garbage collection.

      :returns: float -- Time consumed for GC in ms.
      """
      if self.debug:
         log.msg("NativeWorkerSession.trigger_gc")

      started = rtime()
      gc.collect()
      return 1000. * (rtime() - started)



   @inlineCallbacks
   def start_manhole(self, config, details = None):
      """
      Start a manhole (SSH) within this worker.

      :param config: Manhole configuration.
      :type config: obj
      """
      if not _HAS_MANHOLE:
         emsg = "ERROR: could not start manhole - required packages are missing ({})".format(_MANHOLE_MISSING_REASON)
         log.msg(emsg)
         raise ApplicationError("crossbar.error.feature_unavailable", emsg)

      if self._manhole_service:
         emsg = "ERROR: could not start manhole - already running"
         log.msg(emsg)
         raise ApplicationError("crossbar.error.already_running", emsg)

      try:
         common.config.check_manhole(config)
      except Exception as e:
         emsg = "ERROR: could not start manhole - invalid configuration ({})".format(e)
         log.msg(emsg)
         raise ApplicationError('crossbar.error.invalid_configuration', emsg)

      ## setup user authentication
      ##
      checker = checkers.InMemoryUsernamePasswordDatabaseDontUse()
      for user in config['users']:
         checker.addUser(user['user'], user['password'])

      ## setup manhole namespace
      ##
      namespace = {'worker': self}

      rlm = TerminalRealm()
      rlm.chainedProtocolFactory.protocolFactory = lambda _: ColoredManhole(namespace)

      ptl = portal.Portal(rlm, [checker])

      factory = ConchFactory(ptl)

      try:
         listening_port = yield create_listening_port_from_config(config['endpoint'], factory, self.config.extra.cbdir, reactor)
         self._manhole_service = ManholeService(config, listening_port)

      except Exception as e:
#            emsg = "ERROR: could not connect container component to router - transport establishment failed ({})".format(err.value)
#            log.msg(emsg)
#            raise ApplicationError('crossbar.error.cannot_connect', emsg)
         raise ApplicationError("crossbar.error.cannot_listen", "Could not start manhole: '{}'".format(e))

      else:
         ## publish event "on_manhole_start" to all but the caller
         ##
         topic = 'crossbar.node.{}.worker.{}.on_manhole_start'.format(self.config.extra.node, self.config.extra.worker)
         event = self._manhole_service.marshal()

         #self.publish(topic, event, options = PublishOptions(exclude = [details.caller]))

         returnValue(event)



   @inlineCallbacks
   def stop_manhole(self, details = None):
      """
      Stop Manhole.
      """
      if self._manhole_service:
         yield self._manhole_service.port.stopListening()
         self._manhole_service = None
         topic = 'crossbar.node.{}.worker.{}.on_manhole_stop'.format(self.config.extra.node, self.config.extra.worker)
         self.publish(topic)
      else:
         raise ApplicationError("crossbar.error.could_not_stop", "Could not stop manhole - service is not running")



   def get_manhole(self, details = None):
      if not self._manhole_service:
         return None
      else:
         return self._manhole_service.marshal()



   def get_cpu_affinity(self, details = None):
      """
      Get CPU affinity of this process.

      :returns list -- List of CPU IDs the process affinity is set to.
      """
      if not _HAS_PSUTIL:
         emsg = "ERROR: unable to get CPU affinity - required package 'psutil' is not installed"
         log.msg(emsg)
         raise ApplicationError("crossbar.error.feature_unavailable", emsg)

      try:
         p = psutil.Process(os.getpid())
         current_affinity = p.get_cpu_affinity()
      except Exception as e:
         emsg = "ERROR: could not get CPU affinity ({})".format(e)
         log.msg(emsg)
         raise ApplicationError("crossbar.error.request_error", emsg)
      else:
         res = {'affinity': current_affinity}
         return res



   def set_cpu_affinity(self, cpus, details = None):
      """
      Set CPU affinity of this process.

      :param cpus: List of CPU IDs to set process affinity to.
      :type cpus: list
      """
      if not _HAS_PSUTIL:
         emsg = "ERROR: unable to set CPU affinity - required package 'psutil' is not installed"
         log.msg(emsg)
         raise ApplicationError("crossbar.error.feature_unavailable", emsg)

      try:
         p = psutil.Process(os.getpid())
         p.set_cpu_affinity(cpus)
         new_affinity = p.get_cpu_affinity()
      except Exception as e:
         emsg = "ERROR: could not set CPU affinity ({})".format(e)
         log.msg(emsg)
         raise ApplicationError("crossbar.error.request_error", emsg)
      else:

         ## publish event "on_component_start" to all but the caller
         ##
         topic = 'crossbar.node.{}.worker.{}.on_cpu_affinity_set'.format(self.config.extra.node, self.config.extra.worker)
         res = {'affinity': new_affinity, 'who': details.authid}
         self.publish(topic, res, options = PublishOptions(exclude = [details.caller]))

         return res



   def get_pythonpath(self, details = None):
      """
      Returns the current Python module search paths.

      :returns list -- List of module search paths.
      """
      return sys.path



   def add_pythonpath(self, paths, prepend = True, details = None):
      """
      Add paths to Python module search paths.

      :param paths: List of paths. Relative paths will be resolved relative
                    to the node directory.
      :type paths: list
      :param prepend: If `True`, prepend the given paths to the current paths.
                      Otherwise append.
      :type prepend: bool
      """
      paths_added = []
      for p in paths:
         ## transform all paths (relative to cbdir) into absolute paths
         ##
         path_to_add = os.path.abspath(os.path.join(self.config.extra.cbdir, p))
         if os.path.isdir(path_to_add):
            paths_added.append({'requested': p, 'resolved': path_to_add})
         else:
            emsg = "ERROR: cannot add Python search path '{}' - resolved path '{}' is not a directory".format(p, path_to_add)
            log.msg(emsg)
            raise ApplicationError('crossbar.error.invalid_argument', emsg, requested = p, resolved = path_to_add)

      ## now extend python module search path
      ##
      paths_added_resolved = [p['resolved'] for p in paths_added]
      if prepend:
         sys.path = paths_added_resolved + sys.path
      else:
         sys.path.extend(paths_added_resolved)

      ## publish event "on_pythonpath_add" to all but the caller
      ##
      topic = 'crossbar.node.{}.worker.{}.on_pythonpath_add'.format(self.config.extra.node, self.config.extra.worker)
      res = {
         'paths': sys.path,
         'paths_added': paths_added,
         'prepend': prepend,
         'who': details.authid
      }
      self.publish(topic, res, options = PublishOptions(exclude = [details.caller]))

      return res



   def utcnow(self, details = None):
      """
      Return current time as determined from within this process.

      :returns str -- Current time (UTC) in UTC ISO 8601 format.
      """
      return utcnow()



   def started(self, details = None):
      """
      Return start time of this process.

      :returns str -- Start time (UTC) in UTC ISO 8601 format.
      """
      return utcstr(self._started)



   def uptime(self, details = None):
      """
      Uptime of this process.

      :returns float -- Uptime in seconds.
      """
      now = datetime.utcnow()
      return (now - self._started).total_seconds()




def create_native_worker_server_factory(application_session_factory, ready, exit):
   ## factory that creates router session transports. these are for clients
   ## that talk WAMP-WebSocket over pipes with spawned worker processes and
   ## for any uplink session to a management service
   ##
   factory = NativeWorkerClientFactory(router_session_factory, "ws://localhost", debug = False)

   ## we need to increase the opening handshake timeout in particular, since starting up a worker
   ## on PyPy will take a little (due to JITting)
   factory.setProtocolOptions(failByDrop = False, openHandshakeTimeout = 30, closeHandshakeTimeout = 5)

   return factory