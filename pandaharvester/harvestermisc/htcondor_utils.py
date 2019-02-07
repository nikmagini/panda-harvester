
#=== Imports ===================================================

import re
import time
import datetime
import threading
import random
import xml.etree.ElementTree as ET

try:
    import subprocess32 as subprocess
except Exception:
    import subprocess

try:
    from threading import get_ident
except ImportError:
    from thread import get_ident

import six

from pandaharvester.harvestercore import core_utils
from pandaharvester.harvesterconfig import harvester_config
from pandaharvester.harvestercore.core_utils import SingletonWithID
from pandaharvester.harvestercore.work_spec import WorkSpec
from pandaharvester.harvestercore.fifos import SpecialFIFOBase

# condor python or command api
try:
    import htcondor
except ImportError:
    CONDOR_API = 'command'
else:
    CONDOR_API = 'python'

#===============================================================

#=== Definitions ===============================================

# logger
baseLogger = core_utils.setup_logger('htcondor_utils')





# List of job ads required
CONDOR_JOB_ADS_LIST = [
    'ClusterId', 'ProcId', 'JobStatus', 'LastJobStatus',
    'JobStartDate', 'EnteredCurrentStatus', 'ExitCode',
    'HoldReason', 'LastHoldReason', 'RemoveReason',
]


# harvesterID
harvesterID = harvester_config.master.harvester_id

#===============================================================

#=== Functions =================================================

def _runShell(cmd):
    """
    Run shell function
    """
    cmd = str(cmd)
    p = subprocess.Popen(cmd.split(), shell=False, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdOut, stdErr = p.communicate()
    retCode = p.returncode
    return (retCode, stdOut, stdErr)


def condor_job_id_from_workspec(workspec):
    """
    Generate condor job id with schedd host from workspec
    """
    batchid_str = str(workspec.batchID)
    # backward compatibility if workspec.batchID does not contain ProcId
    if '.' not in batchid_str:
        batchid_str += '.0'
    return '{0}#{1}'.format(workspec.submissionHost, batchid_str)


def get_host_batchid_map(workspec_list):
    """
    Get a dictionary of submissionHost: list of batchIDs from workspec_list
    return {submissionHost_1: {batchID_1_1, ...}, submissionHost_2: {...}, ...}
    """
    host_batchid_map = {}
    for workspec in workspec_list:
        host = workspec.submissionHost
        batchid_str = str(workspec.batchID)
        # backward compatibility if workspec.batchID does not contain ProcId
        if '.' not in batchid_str:
            batchid_str += '.0'
        try:
            host_batchid_map[host].append(batchid_str)
        except KeyError:
            host_batchid_map[host] = [batchid_str]
    return host_batchid_map


def get_batchid_from_job(job_ads_dict):
    """
    Get batchID string from condor job dict
    """
    batchid = '{0}.{1}'.format(job_ads_dict['ClusterId'], job_ads_dict['ProcId'])
    return batchid


def get_job_id_tuple_from_batchid(batchid):
    """
    Get tuple (ClusterId, ProcId) from batchID string
    """
    batchid_str_list = str(batchid).split('.')
    clusterid = batchid_str_list[0]
    procid = batchid_str_list[1]
    return (clusterid, procid)

#===============================================================

#=== Classes ===================================================

# Condor queue cache fifo
class CondorQCacheFifo(six.with_metaclass(SingletonWithID, SpecialFIFOBase)):
    global_lock_id = -1

    def __init__(self, target, *args, **kwargs):
        name_suffix = target.split('.')[0]
        self.titleName = 'CondorQCache_{0}'.format(name_suffix)
        SpecialFIFOBase.__init__(self)

    def lock(self, score=None):
        lock_key = format(int(random.random() * 2**32), 'x')
        if score is None:
            score = time.time()
        retVal = self.putbyid(self.global_lock_id, lock_key, score)
        if retVal:
            return lock_key
        return None

    def unlock(self, key=None, force=False):
        peeked_tuple = self.peekbyid(id=self.global_lock_id)
        if peeked_tuple.score is None or peeked_tuple.item is None:
            return True
        elif force or self.decode(peeked_tuple.item) == key:
            self.release([self.global_lock_id])
            return True
        else:
            return False


# Condor client
class CondorClient(object):
    @classmethod
    def renew_session_and_retry(cls, func):
        """
        If RuntimeError, call renew_session and retry
        """
        # Wrapper
        def wrapper(self, *args, **kwargs):
            # Make logger
            tmpLog = core_utils.make_logger(baseLogger, 'submissionHost={0}'.format(self.submissionHost), method_name='CondorClient.renew_session_if_error')
            func_name = func.__name__
            try:
                ret = func(self, *args, **kwargs)
            except RuntimeError as e:
                tmpLog.debug('got RuntimeError: {0}'.format(e))
                if self.lock.acquire(False):
                    self.renew_session()
                    self.lock.release()
                tmpLog.debug('condor session renewed. Retrying {0}'.format(func_name))
                ret = func(self, analyzer, *args, **kwargs)
                tmpLog.debug('done')
            return ret
        return wrapper

    def __init__(self, submissionHost, *args, **kwargs):
        self.submissionHost = submissionHost
        # Make logger
        tmpLog = core_utils.make_logger(baseLogger, 'submissionHost={0}'.format(self.submissionHost), method_name='CondorClient.__init__')
        # Initialize
        tmpLog.debug('Initializing client')
        self.lock = threading.Lock()
        self.condor_api = CONDOR_API
        self.condor_schedd = None
        self.condor_pool = None
        # Parse condor command remote options from workspec
        if self.submissionHost in ('LOCAL', 'None'):
            tmpLog.debug('submissionHost is {0}, treated as local schedd. Skipped'.format(self.submissionHost))
        else:
            try:
                self.condor_schedd, self.condor_pool = self.submissionHost.split(',')[0:2]
            except ValueError:
                tmpLog.error('Invalid submissionHost: {0} . Skipped'.format(self.submissionHost))
        # Use Python API or fall back to command
        if self.condor_api == 'python':
            try:
                self.secman = htcondor.SecMan()
                self.renew_session()
            except Exception as e:
                self.condor_api = 'command'
                tmpLog.warning('Using condor command instead due to exception from unsupported version of python or condor api: {0}'.format(e))
        tmpLog.debug('Initialized client')

    def renew_session(self, retry=3):
        # Make logger
        tmpLog = core_utils.make_logger(baseLogger, 'submissionHost={0}'.format(self.submissionHost), method_name='CondorClient.renew_session')
        # Clear security session
        tmpLog.info('Renew condor session')
        self.secman.invalidateAllSessions()
        # Recreate collector and schedd object
        i_try = 1
        while i_try <= retry:
            try:
                tmpLog.info('Try {0}'.format(i_try))
                if self.condor_pool:
                    self.collector = htcondor.Collector(self.condor_pool)
                else:
                    self.collector = htcondor.Collector()
                if self.condor_schedd:
                    self.scheddAd = self.collector.locate(htcondor.DaemonTypes.Schedd, self.condor_schedd)
                else:
                    self.scheddAd = self.collector.locate(htcondor.DaemonTypes.Schedd)
                self.schedd = htcondor.Schedd(self.scheddAd)
                tmpLog.info('Success')
                break
            except Exception as e:
                tmpLog.warning('Recreate condor collector and schedd failed: {0}'.format(e))
                if i_try < retry:
                    tmpLog.warning('Failed. Retry...')
                else:
                    tmpLog.warning('Retry {0} times. Still failed. Skipped'.format(i_try))
                i_try += 1
                self.secman.invalidateAllSessions()
                time.sleep(3)
        # Sleep
        time.sleep(3)


# Condor job query
class CondorJobQuery(six.with_metaclass(SingletonWithID, CondorClient)):
    # class lock
    classLock = threading.Lock()
    # Query commands
    orig_comStr_list = [
        'condor_q -xml',
        'condor_history -xml',
    ]
    # Bad text of redundant xml roots to eleminate from condor XML
    badtext = """
</classads>

<?xml version="1.0"?>
<!DOCTYPE classads SYSTEM "classads.dtd">
<classads>
"""

    def __init__(self, cacheEnable=False, cacheRefreshInterval=None, useCondorHistory=True, *args, **kwargs):
        self.submissionHost = str(kwargs.get('id'))
        # Make logger
        tmpLog = core_utils.make_logger(baseLogger, 'submissionHost={0}'.format(self.submissionHost), method_name='CondorJobQuery.__init__')
        # Initialize
        with self.classLock:
            tmpLog.debug('Start')
            CondorClient.__init__(self, self.submissionHost, *args, **kwargs)
            # For condor_q cache
            self.cacheEnable = cacheEnable
            if self.cacheEnable:
                self.cache = ([], 0)
                self.cacheRefreshInterval = cacheRefreshInterval
            self.useCondorHistory = useCondorHistory
            tmpLog.debug('Initialize done')

    def get_all(self, batchIDs_list=[]):
        # Make logger
        tmpLog = core_utils.make_logger(baseLogger, 'submissionHost={0}'.format(self.submissionHost), method_name='CondorJobQuery.get_all')
        # Get all
        tmpLog.debug('Start')
        job_ads_all_dict = {}
        if self.condor_api == 'python':
            try:
                job_ads_all_dict = self.query_with_python(batchIDs_list)
            except Exception as e:
                tmpLog.warning('Using condor command instead due to exception from unsupported version of python or condor api, or code bugs: {0}'.format(e))
                job_ads_all_dict = self.query_with_command(batchIDs_list)
        else:
            job_ads_all_dict = self.query_with_command(batchIDs_list)
        return job_ads_all_dict

    def query_with_command(self, batchIDs_list=[]):
        # Make logger
        tmpLog = core_utils.make_logger(baseLogger, 'submissionHost={0}'.format(self.submissionHost), method_name='CondorJobQuery.query_with_command')
        # Start query
        tmpLog.debug('Start query')
        job_ads_all_dict = {}
        batchIDs_set = set(batchIDs_list)
        for orig_comStr in self.orig_comStr_list:
            # String of batchIDs
            batchIDs_str = ' '.join(list(batchIDs_set))
            # Command
            if 'condor_q' in orig_comStr or ('condor_history' in orig_comStr and batchIDs_set):
                name_opt = '-name {0}'.format(self.condor_schedd) if self.condor_schedd else ''
                pool_opt = '-pool {0}'.format(self.condor_pool) if self.condor_pool else ''
                ids = batchIDs_str
                comStr = '{cmd} {name_opt} {pool_opt} {ids}'.format(cmd=orig_comStr,
                                                                    name_opt=name_opt,
                                                                    pool_opt=pool_opt,
                                                                    ids=ids)
            else:
                # tmpLog.debug('No batch job left to query in this cycle by this thread')
                continue
            tmpLog.debug('check with {0}'.format(comStr))
            (retCode, stdOut, stdErr) = _runShell(comStr)
            if retCode == 0:
                # Command succeeded
                job_ads_xml_str = '\n'.join(str(stdOut).split(self.badtext))
                if '<c>' in job_ads_xml_str:
                    # Found at least one job
                    # XML parsing
                    xml_root = ET.fromstring(job_ads_xml_str)
                    def _getAttribute_tuple(attribute_xml_element):
                        # Attribute name
                        _n = str(attribute_xml_element.get('n'))
                        # Attribute value text
                        _t = ' '.join(attribute_xml_element.itertext())
                        return (_n, _t)
                    # Every batch job
                    for _c in xml_root.findall('c'):
                        job_ads_dict = dict()
                        # Every attribute
                        attribute_iter = map(_getAttribute_tuple, _c.findall('a'))
                        job_ads_dict.update(attribute_iter)
                        batchid = get_batchid_from_job(job_ads_dict)
                        condor_job_id = '{0}#{1}'.format(self.submissionHost, batchid)
                        job_ads_all_dict[condor_job_id] = job_ads_dict
                        # Remove batch jobs already gotten from the list
                        if batchid in batchIDs_set:
                            batchIDs_set.discard(batchid)
                else:
                    # Job not found
                    tmpLog.debug('job not found with {0}'.format(comStr))
                    continue
            else:
                # Command failed
                errStr = 'command "{0}" failed, retCode={1}, error: {2} {3}'.format(comStr, retCode, stdOut, stdErr)
                tmpLog.error(errStr)
        if len(batchIDs_set) > 0:
            # Job unfound via both condor_q or condor_history, marked as unknown worker in harvester
            for batchid in batchIDs_set:
                condor_job_id = '{0}#{1}'.format(self.submissionHost, batchid)
                job_ads_all_dict[condor_job_id] = dict()
            tmpLog.info( 'Unfound batch jobs of submissionHost={0}: {1}'.format(
                            self.submissionHost, ' '.join(list(batchIDs_set)) ) )
        # Return
        return job_ads_all_dict

    @CondorClient.renew_session_and_retry
    def query_with_python(self, batchIDs_list=[]):
        # Make logger
        tmpLog = core_utils.make_logger(baseLogger, 'submissionHost={0}'.format(self.submissionHost), method_name='CondorJobQuery.query_with_python')
        # Start query
        tmpLog.debug('Start query')
        cache_fifo = CondorQCacheFifo(target=self.submissionHost, id='{0},{1}'.format(self.submissionHost, get_ident()))
        job_ads_all_dict = {}
        # make id sets
        batchIDs_set = set(batchIDs_list)
        clusterids_set = set([get_job_id_tuple_from_batchid(batchid)[0] for batchid in batchIDs_list])
        # query from cache
        def cache_query(requirements=None, projection=CONDOR_JOB_ADS_LIST, timeout=60):
            # query from condor xquery and update cache to fifo
            def update_cache(lockInterval=90):
                tmpLog.debug('update_cache')
                # acquire lock with score timestamp
                score = time.time() - self.cacheRefreshInterval + lockInterval
                lock_key = cache_fifo.lock(score=score)
                if lock_key is not None:
                    # acquired lock, update from condor schedd
                    tmpLog.debug('got lock, updating cache')
                    jobs_iter_orig = self.schedd.xquery(requirements=requirements, projection=projection)
                    jobs_iter = [ dict(job) for job in jobs_iter_orig ]
                    timeNow = time.time()
                    cache_fifo.put(jobs_iter, timeNow)
                    self.cache = (jobs_iter, timeNow)
                    # release lock
                    retVal = cache_fifo.unlock(key=lock_key)
                    if retVal:
                        tmpLog.debug('done update cache and unlock')
                    else:
                        tmpLog.warning('cannot unlock... Maybe something wrong')
                    return jobs_iter
                else:
                    tmpLog.debug('cache fifo locked by other thread. Skipped')
                    return None
            # remove invalid or outdated caches from fifo
            def cleanup_cache(timeout=60):
                tmpLog.debug('cleanup_cache')
                id_list = list()
                attempt_timestamp = time.time()
                n_cleanup = 0
                while True:
                    if time.time() > attempt_timestamp + timeout:
                        tmpLog.debug('time is up when cleanup cache. Skipped')
                        break
                    peeked_tuple = cache_fifo.peek(skip_item=True)
                    if peeked_tuple is None:
                        tmpLog.debug('empty cache fifo')
                        break
                    elif peeked_tuple.score is not None \
                        and time.time() <= peeked_tuple.score + self.cacheRefreshInterval:
                        tmpLog.debug('nothing expired')
                        break
                    elif peeked_tuple.id is not None:
                        retVal = cache_fifo.release([peeked_tuple.id])
                        if isinstance(retVal, int):
                            n_cleanup += retVal
                    else:
                        # problematic
                        tmpLog.warning('got nothing when cleanup cache, maybe problematic. Skipped')
                        break
                tmpLog.debug('cleaned up {0} objects in cache fifo'.format(n_cleanup))
            # start
            jobs_iter = tuple()
            try:
                attempt_timestamp = time.time()
                while True:
                    if time.time() > attempt_timestamp + timeout:
                        # skip cache_query if too long
                        tmpLog.debug('cache_query got timeout ({0} seconds). Skipped '.format(timeout))
                        break
                    # get latest cache
                    peeked_tuple = cache_fifo.peeklast(skip_item=True)
                    if peeked_tuple is not None and peeked_tuple.score is not None:
                        # got something
                        if peeked_tuple.id == cache_fifo.global_lock_id:
                            if time.time() <= peeked_tuple.score + self.cacheRefreshInterval:
                                # lock
                                tmpLog.debug('got fifo locked. Wait and retry...')
                                time.sleep(random.uniform(1, 5))
                                continue
                            else:
                                # expired lock
                                tmpLog.debug('got lock expired. Clean up and retry...')
                                cleanup_cache()
                                continue
                        elif time.time() <= peeked_tuple.score + self.cacheRefreshInterval:
                            # got valid cache
                            _obj, _last_update = self.cache
                            if _last_update >= peeked_tuple.score:
                                # valid local cache
                                tmpLog.debug('valid local cache')
                                jobs_iter = _obj
                            else:
                                # valid fifo cache
                                tmpLog.debug('update local cache from fifo')
                                peeked_tuple_with_item = cache_fifo.peeklast()
                                if peeked_tuple_with_item is not None \
                                    and peeked_tuple.id != cache_fifo.global_lock_id \
                                    and peeked_tuple_with_item.item is not None:
                                    jobs_iter = cache_fifo.decode(peeked_tuple_with_item.item)
                                    self.cache = (jobs_iter, peeked_tuple_with_item.score)
                                else:
                                    tmpLog.debug('peeked invalid cache fifo object. Wait and retry...')
                                    time.sleep(random.uniform(1, 5))
                                    continue
                        else:
                            # cache expired
                            tmpLog.debug('update cache in fifo')
                            retVal = update_cache()
                            if retVal is not None:
                                jobs_iter = retVal
                            cleanup_cache()
                        break
                    else:
                        # no cache in fifo, check with size again
                        if cache_fifo.size() == 0:
                            if time.time() > attempt_timestamp + random.uniform(10, 30):
                                # have waited for long enough, update cache
                                tmpLog.debug('waited enough, update cache in fifo')
                                retVal = update_cache()
                                if retVal is not None:
                                    jobs_iter = retVal
                                break
                            else:
                                # still nothing, wait
                                time.sleep(2)
                        continue
            except Exception as _e:
                tmpLog.error('Error querying from cache fifo; {0}'.format(_e))
            return jobs_iter
        # query method options
        query_method_list = [self.schedd.xquery]
        if self.cacheEnable:
            query_method_list.insert(0, cache_query)
        if self.useCondorHistory:
            query_method_list.append(self.schedd.history)
        # Go
        for query_method in query_method_list:
            # Make requirements
            clusterids_str = ','.join(list(clusterids_set))
            if query_method is cache_query:
                requirements = 'harvesterID =?= "{0}"'.format(harvesterID)
            else:
                requirements = 'member(ClusterID, {{{0}}})'.format(clusterids_str)
            tmpLog.debug('Query method: {0} ; clusterids: "{1}"'.format(query_method.__name__, clusterids_str))
            # Query
            jobs_iter = query_method(requirements=requirements, projection=CONDOR_JOB_ADS_LIST)
            for job in jobs_iter:
                job_ads_dict = dict(job)
                batchid = get_batchid_from_job(job_ads_dict)
                condor_job_id = '{0}#{1}'.format(self.submissionHost, batchid)
                job_ads_all_dict[condor_job_id] = job_ads_dict
                # Remove batch jobs already gotten from the list
                batchIDs_set.discard(batchid)
            if len(batchIDs_set) == 0:
                break
        # Remaining
        if len(batchIDs_set) > 0:
            # Job unfound via both condor_q or condor_history, marked as unknown worker in harvester
            for batchid in batchIDs_set:
                condor_job_id = '{0}#{1}'.format(self.submissionHost, batchid)
                job_ads_all_dict[condor_job_id] = dict()
            tmpLog.info( 'Unfound batch jobs of submissionHost={0}: {1}'.format(
                            self.submissionHost, ' '.join(list(batchIDs_set)) ) )
        # Return
        return job_ads_all_dict


# Condor job submit
class CondorJobSubmit(six.with_metaclass(SingletonWithID, CondorClient)):
    # class lock
    classLock = threading.Lock()

    def __init__(self, *args, **kwargs):
        self.submissionHost = str(kwargs.get('id'))
        # Make logger
        tmpLog = core_utils.make_logger(baseLogger, 'submissionHost={0}'.format(self.submissionHost), method_name='CondorJobSubmit.__init__')
        # Initialize
        with self.classLock:
            tmpLog.debug('Start')
            self.lock = threading.Lock()
            CondorClient.__init__(self, self.submissionHost, *args, **kwargs)

    def submit(self, jdl_str, use_spool=False):
        # Make logger
        tmpLog = core_utils.make_logger(baseLogger, 'submissionHost={0}'.format(self.submissionHost), method_name='CondorJobSubmit.submit')
        # Get all
        tmpLog.debug('Start')
        job_ads_all_dict = {}
        if self.condor_api == 'python':
            try:
                job_ads_all_dict = self.query_with_python(batchIDs_list)
            except RuntimeError as e:
                tmpLog.error(e)
                if self.lock.acquire(False):
                    self.renew_session()
                    self.lock.release()
            except Exception as e:
                tmpLog.warning('Using condor command instead due to exception from unsupported version of python or condor api: {0}'.format(e))
                job_ads_all_dict = self.query_with_command(batchIDs_list)
        else:
            job_ads_all_dict = self.query_with_command(batchIDs_list)
        return job_ads_all_dict

    def submit_with_command(self, jdl_str, use_spool=False):
        # Make logger
        tmpLog = core_utils.make_logger(baseLogger, 'submissionHost={0}'.format(self.submissionHost), method_name='CondorJobSubmit.submit_with_command')
        # # Start query
        # tmpLog.debug('Start query')
        # job_ads_all_dict = {}
        # batchIDs_set = set(batchIDs_list)
        # for orig_comStr in self.orig_comStr_list:
        #     # String of batchIDs
        #     batchIDs_str = ' '.join(list(batchIDs_set))
        #     # Command
        #     if 'condor_q' in orig_comStr or ('condor_history' in orig_comStr and batchIDs_set):
        #         name_opt = '-name {0}'.format(self.condor_schedd) if self.condor_schedd else ''
        #         pool_opt = '-pool {0}'.format(self.condor_pool) if self.condor_pool else ''
        #         ids = batchIDs_str
        #         comStr = '{cmd} {name_opt} {pool_opt} {ids}'.format(cmd=orig_comStr,
        #                                                             name_opt=name_opt,
        #                                                             pool_opt=pool_opt,
        #                                                             ids=ids)
        #     else:
        #         # tmpLog.debug('No batch job left to query in this cycle by this thread')
        #         continue
        #     tmpLog.debug('check with {0}'.format(comStr))
        #     (retCode, stdOut, stdErr) = _runShell(comStr)
        #     if retCode == 0:
        #         # Command succeeded
        #         job_ads_xml_str = '\n'.join(str(stdOut).split(self.badtext))
        #         if '<c>' in job_ads_xml_str:
        #             # Found at least one job
        #             # XML parsing
        #             xml_root = ET.fromstring(job_ads_xml_str)
        #             def _getAttribute_tuple(attribute_xml_element):
        #                 # Attribute name
        #                 _n = str(attribute_xml_element.get('n'))
        #                 # Attribute value text
        #                 _t = ' '.join(attribute_xml_element.itertext())
        #                 return (_n, _t)
        #             # Every batch job
        #             for _c in xml_root.findall('c'):
        #                 job_ads_dict = dict()
        #                 # Every attribute
        #                 attribute_iter = map(_getAttribute_tuple, _c.findall('a'))
        #                 job_ads_dict.update(attribute_iter)
        #                 batchid = str(job_ads_dict['ClusterId'])
        #                 condor_job_id = '{0}#{1}'.format(self.submissionHost, batchid)
        #                 job_ads_all_dict[condor_job_id] = job_ads_dict
        #                 # Remove batch jobs already gotten from the list
        #                 if batchid in batchIDs_set:
        #                     batchIDs_set.discard(batchid)
        #         else:
        #             # Job not found
        #             tmpLog.debug('job not found with {0}'.format(comStr))
        #             continue
        #     else:
        #         # Command failed
        #         errStr = 'command "{0}" failed, retCode={1}, error: {2} {3}'.format(comStr, retCode, stdOut, stdErr)
        #         tmpLog.error(errStr)
        # if len(batchIDs_set) > 0:
        #     # Job unfound via both condor_q or condor_history, marked as unknown worker in harvester
        #     for batchid in batchIDs_set:
        #         condor_job_id = '{0}#{1}'.format(self.submissionHost, batchid)
        #         job_ads_all_dict[condor_job_id] = dict()
        #     tmpLog.info( 'Unfound batch jobs of submissionHost={0}: {1}'.format(
        #                     self.submissionHost, ' '.join(list(batchIDs_set)) ) )
        # # Return
        # return job_ads_all_dict
        pass


    @CondorClient.renew_session_and_retry
    def submit_with_python(self, jdl_str, use_spool=False):
        # Make logger
        tmpLog = core_utils.make_logger(baseLogger, 'submissionHost={0}'.format(self.submissionHost), method_name='CondorJobSubmit.submit_with_python')
        # Start
        tmpLog.debug('Start')
        # Make submit object
        submit_obj = htcondor.Submit(jdl_str)
        with schedd.transaction() as txn:
            submit_obj.queue(txn)

        # Go
        for query_method in query_method_list:
            # Make requirements
            batchIDs_str = ','.join(list(batchIDs_set))
            if query_method is cache_query:
                requirements = 'harvesterID =?= "{0}"'.format(harvesterID)
            else:
                requirements = 'member(ClusterID, {{{0}}})'.format(batchIDs_str)
            tmpLog.debug('Query method: {0} ; batchIDs: "{1}"'.format(query_method.__name__, batchIDs_str))
            # Query
            jobs_iter = query_method(requirements=requirements, projection=CONDOR_JOB_ADS_LIST)
            for job in jobs_iter:
                job_ads_dict = dict(job)
                batchid = str(job_ads_dict['ClusterId'])
                condor_job_id = '{0}#{1}'.format(self.submissionHost, batchid)
                job_ads_all_dict[condor_job_id] = job_ads_dict
                # Remove batch jobs already gotten from the list
                batchIDs_set.discard(batchid)
            if len(batchIDs_set) == 0:
                break
        # Remaining
        if len(batchIDs_set) > 0:
            # Job unfound via both condor_q or condor_history, marked as unknown worker in harvester
            for batchid in batchIDs_set:
                condor_job_id = '{0}#{1}'.format(self.submissionHost, batchid)
                job_ads_all_dict[condor_job_id] = dict()
            tmpLog.info( 'Unfound batch jobs of submissionHost={0}: {1}'.format(
                            self.submissionHost, ' '.join(list(batchIDs_set)) ) )
        # Return
        return job_ads_all_dict

#===============================================================