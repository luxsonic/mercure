

from dataclasses import dataclass
import dataclasses
import os
from pathlib import Path
import shutil
from typing import Any, Dict, Generator, List, Optional, Tuple, Type, Union, cast
import typing


from dicomweb_client import DICOMfileClient
from common.types import DicomTarget, DicomWebTarget, FolderTarget
from dispatch.target_types.base import ProgressInfo
from dispatch.target_types.registry import get_handler
# Standard python includes
from datetime import datetime
import time
# Starlette-related includes

# App-specific includes
import common.config as config
from webinterface.common import redis, rq_fast_queue, rq_slow_queue
from rq.job import Dependency, JobStatus, Job
from rq import Connection, Queue, get_current_job

logger = config.get_logger()



def query_dummy(job_id, job_kwargs):
    """
    Dummy function to simulate a long-running task.
    """
    total_time = 2  # Total time for the job in seconds (1 minute)
    update_interval = 0.25  # Interval between updates in seconds
    remaining = total_time // update_interval
    completed = 0
    start_time = time.monotonic()

    while (time.monotonic() - start_time) < total_time:
        time.sleep(update_interval)  # Sleep for the interval duration
        out_file = (Path(job_kwargs['path']) / f"dummy{completed}_{job_id}.dcm")
        if out_file.exists():
            raise Exception(f"{out_file} exists already")
        out_file.touch()
        remaining -= 1
        completed += 1

        yield completed, remaining, f"{completed} / {remaining + completed}"


@dataclass
class ClassBasedRQJob():
    parent: Optional[str] = None
    type: str = "unknown"
    _job: Optional[Job] = None
    _queue: str = ''

    @classmethod
    def queue(cls) -> Queue:
        return Queue(cls._queue, connection=redis)

    def create(self, rq_options={}, **kwargs) -> Job:
        fields = dataclasses.fields(self)
        meta = {field.name: getattr(self, field.name) for field in fields}
        return Job.create(self._execute, kwargs=kwargs, meta=meta, **rq_options)

    @classmethod
    def _execute(cls, **kwargs) -> Any:
        job = get_current_job()
        if not job:
            raise Exception("No current job")
        fields = dataclasses.fields(cls)
        meta = {}
        for f in fields:
            if f.name in job.meta and not f.name.startswith('_'):
                meta[f.name] = job.meta[f.name]
        result = cls(**meta, _job=job).execute(**kwargs)
        if result is None:
            return b""
        return result

    def execute(self, *args, **kwargs) -> Any:
        pass
    
    @staticmethod
    def move_to_destination(path, destination, job_id) -> None:
        if destination is None:
            config.read_config()
            for p in Path(path).glob("**/*"):
                if p.is_file():
                    shutil.move(str(p), config.mercure.incoming_folder) # Move the file to incoming folder
            # tree(config.mercure.incoming_folder)
            shutil.rmtree(path)
        else:
            dest_folder: Path = Path(destination) / job_id
            dest_folder.mkdir(exist_ok=True)
            logger.info(f"moving {path} to {dest_folder}")
            shutil.move(path, dest_folder)
            # tree(dest_folder)
            logger.info(f"moved")

@dataclass 
class CheckAccessionsJob(ClassBasedRQJob):
    type: str = "check_accessions"
    _queue: str = rq_fast_queue.name
    
    def execute(self, *, accessions: List[str], node: Union[DicomTarget, DicomWebTarget], search_filters:Dict[str,List[str]]={}):
        """
        Check if the given accessions exist on the node using a DICOM query.
        """
        # c = SimpleDicomClient(node.ip, node.port, node.aet_target, None)
        # assert self.parent is not None
        results = []
        try:
            for accession in accessions:
                found_ds_list = get_handler(node).find_from_target(node, accession, search_filters)
                if not found_ds_list:
                    raise ValueError("No series found with accession number {}".format(accession))
                results.extend(found_ds_list)
            return results
        except Exception as e:
            if not self._job:
                raise
            self._job.meta['failed_reason'] = str(e)
            self._job.save_meta() # type: ignore
            if self.parent and (job_parent := Job.fetch(self.parent)):
                job_parent.meta['failed_reason'] = e.args[0]
                job_parent.save_meta() # type: ignore
                Queue(job_parent.origin)._enqueue_job(job_parent,at_front=True)

            raise


@dataclass
class GetAccessionJob(ClassBasedRQJob):
    type: str = "get_accession"
    paused: bool = False
    offpeak: bool = False
    _queue: str = rq_slow_queue.name

    @classmethod
    def get_accession(cls, job_id, accession: str, node: Union[DicomTarget, DicomWebTarget], search_filters: Dict[str, List[str]], path) -> Generator[ProgressInfo, None, None]:
        yield from get_handler(node).get_from_target(node, accession, search_filters, path)

    def execute(self, *, accession:str, node: Union[DicomTarget, DicomWebTarget], search_filters:Dict[str, List[str]], path: str):
        print(f"Getting {accession}")
        job = cast(Job,self._job)
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            job_parent = None
            if parent_id := self.parent:
                job_parent = Job.fetch(parent_id)

            if job_parent:
                job_parent.meta['started'] = job_parent.meta.get('started',0) + 1
                job_parent.save_meta() # type: ignore

            job.meta['started'] = 1
            job.meta['progress'] = "0 / Unknown"
            job.save_meta() # type: ignore
            for info in self.get_accession(job.id, accession=accession, node=node, search_filters=search_filters, path=path):
                job.meta['remaining'] = info.remaining
                job.meta['completed'] = info.completed 
                job.meta['progress'] = info.progress
                job.save_meta() # type: ignore  # Save the updated meta data to the job
                logger.info(info.progress)
            if job_parent:
                if job_parent.kwargs["move_promptly"]:
                    self.move_to_destination(path, job_parent.kwargs["destination"], job_parent.id)

                job_parent.get_meta() # there is technically a race condition here...
                job_parent.meta['completed'] += 1
                job_parent.meta['progress'] = f"{job_parent.meta['started'] } / {job_parent.meta['completed'] } / {job_parent.meta['total']}"
                job_parent.save_meta() # type: ignore
        except:
            if not job_parent:
                raise
            # Cancel remaining sibling jobs
            logger.info("Cancelling sibling jobs.")
            for subjob_id in job_parent.kwargs.get('subjobs',[]):
                if subjob_id == job.id:
                    continue
                subjob = Job.fetch(subjob_id)
                if subjob.get_status() not in ('finished', 'canceled','failed'):
                    subjob.cancel()
            job_parent.get_meta() 
            logger.info("Cancelled sibling jobs.")
            job_parent.meta["failed_reason"] = f"Failed to retrieve {accession}"
            Queue(job_parent.origin)._enqueue_job(job_parent,at_front=True) # Force the parent job to run and fail itself
            raise

        return "Job complete"

@dataclass
class MainJob(ClassBasedRQJob):
    type: str = "batch" 
    started: int = 0
    completed: int = 0
    total: int = 0
    paused: bool = False 
    offpeak: bool = False
    _queue: str = rq_fast_queue.name

    def execute(self, *, accessions, subjobs, path, destination, move_promptly) -> str:
        job = cast(Job,self._job)
        job.get_meta()
        for job_id in job.kwargs.get('subjobs',[]):
            subjob = Job.fetch(job_id)
            if (status := subjob.get_status()) != 'finished':
                raise Exception(f"Subjob {subjob.id} is {status}")
            if job.kwargs.get('failed', False):
                raise Exception(f"Failed")

        logger.info(f"Job completing {job.id}")

        if not move_promptly:
            logger.info("Moving files during completion as move_promptly==False")
            for p in Path(path).iterdir():
                if not p.is_dir():
                    continue
                self.move_to_destination(p, destination, job.id)

        # subprocess.run(["./bin/ubuntu22.04/getdcmtags", filename, self.called_aet, "MERCURE"],check=True)

        logger.info(f"Removing job directory {path}")
        # tree(destination)
        shutil.rmtree(path)

        return "Job complete"

class WrappedJob():
    job: Job
    def __init__(self, job: Union[Job,str]):
        if isinstance(job, str):
            if not (result:=Job.fetch(job)):
                raise Exception("Invalid Job ID")
            self.job = result
        else:
            self.job = job
        
        assert self.job.meta.get('type') == 'batch', f"Job type must be batch, got {self.job.meta['type']}"

    @classmethod
    def create(cls, accessions, search_filters:Dict[str, List[str]], dicom_node: Union[DicomWebTarget, DicomTarget], destination_path, offpeak=False) -> 'WrappedJob':
        """
        Create a job to process the given accessions and store them in the specified destination path.
        """

        with Connection(redis):
            get_accession_jobs: List[Job] = []
            check_job = CheckAccessionsJob().create(accessions=accessions, search_filters=search_filters, node=dicom_node)
            for accession in accessions:
                job = GetAccessionJob(offpeak=offpeak).create(
                    accession=accession, 
                    node=dicom_node,
                    search_filters=search_filters,
                    rq_options=dict(
                        depends_on=cast(List[Union[Dependency, Job]],[check_job]),
                        timeout=30*60,
                        result_ttl=-1
                        )
                    )
                get_accession_jobs.append(job)
            depends = Dependency(
                jobs=cast(List[Union[Job,str]],get_accession_jobs),
                allow_failure=True,    # allow_failure defaults to False
            )
            full_job = MainJob(total=len(get_accession_jobs), offpeak=offpeak).create(
                accessions = accessions,
                subjobs = [j.id for j in get_accession_jobs],
                destination = destination_path,
                move_promptly = True,
                rq_options = dict(depends_on=depends, timeout=-1, result_ttl=-1)
            )
            check_job.meta["parent"] = full_job.id
            for j in get_accession_jobs:
                j.meta["parent"] = full_job.id
                j.kwargs["path"] = Path(config.mercure.jobs_folder) / full_job.id / j.kwargs['accession']
                j.kwargs["path"].mkdir(parents=True)

            full_job.kwargs["path"] = Path(config.mercure.jobs_folder) / full_job.id

        CheckAccessionsJob.queue().enqueue_job(check_job)
        for j in get_accession_jobs:
            GetAccessionJob.queue().enqueue_job(j)
        MainJob.queue().enqueue_job(full_job)

        wrapped_job = WrappedJob(full_job)
        if offpeak and not _is_offpeak(config.mercure.offpeak_start, config.mercure.offpeak_end, datetime.now().time()):
            wrapped_job.pause()

        return wrapped_job

    def __bool__(self) -> bool:
        return bool(self.job)

    def pause(self) -> None:
        """
        Pause the current job, including all its subjobs.
        """
        for job_id in self.job.kwargs.get('subjobs',[]):
            subjob = Job.fetch(job_id)
            if subjob and (subjob.is_deferred or subjob.is_queued):
                subjob.meta['paused'] = True
                subjob.save_meta() # type: ignore
                subjob.cancel()
        self.job.get_meta()
        self.job.meta['paused'] = True
        self.job.save_meta() # type: ignore

    def resume(self) -> None:
        """
        Resume a paused job by unpausing all its subjobs
        """
        for subjob_id in self.job.kwargs.get('subjobs',[]):
            subjob = Job.fetch(subjob_id)
            if subjob and subjob.meta.get('paused', None):
                subjob.meta['paused'] = False
                subjob.save_meta() # type: ignore
                Queue(subjob.origin).canceled_job_registry.requeue(subjob_id)
        self.job.get_meta()
        self.job.meta['paused'] = False
        self.job.save_meta() # type: ignore

    def retry(self) -> None:
        """
        Retry a failed job by enqueuing it again
        """
        # job.meta["retries"] = job.meta.get("retries", 0) + 1
        # if job.meta["retries"] > 3:
        #     return False
        logger.info(f"Retrying {self.job}")
        for subjob in self.get_subjobs():
            if (status:=self.job.get_status()) in ("failed", "canceled"):
                logger.info(f"Retrying {subjob}")
                if status == "failed" and (job_path:=Path(subjob.kwargs['path'])).exists():
                    shutil.rmtree(job_path) # Clean up after a failed job
                Queue(subjob.origin).enqueue_job(subjob)
        Queue(self.job.origin).enqueue_job(self.job)

    @classmethod
    def update_all_jobs_offpeak(cls) -> None:
        """
        Resume or pause offpeak jobs based on whether the current time is within offpeak hours.
        """
        config.read_config()
        is_offpeak = _is_offpeak(config.mercure.offpeak_start, config.mercure.offpeak_end, datetime.now().time())
        logger.info(f"is_offpeak {is_offpeak}")
        for job in WrappedJob.get_all_jobs():
            job.update_offpeak(is_offpeak)

    def update_offpeak(self, is_offpeak) -> None:
        if not self.meta.get("offpeak"):
            return
        if self.get_status() not in ("waiting", "running", "queued", "deferred"):
            return

        if is_offpeak:
            # logger.info(f"{job.meta}, {job.get_status()}")
            if self.is_paused:
                logger.info("Resuming")
                self.resume()
        else:
            if not self.is_paused:
                logger.info("Pausing")
                self.pause()

    def get_subjobs(self) -> Generator[Job, None, None]:
        return (Job.fetch(job) for job in self.job.kwargs.get('subjobs', []) if job)

    def get_status(self) -> JobStatus:
        return cast(JobStatus,self.job.get_status())

    def get_meta(self) -> Any:
        return cast(dict,self.job.get_meta())
        
    @property
    def meta(self) -> typing.Dict:
        return cast(dict, self.job.meta)
    
    @property
    def is_failed(self) -> bool:
        return cast(bool,self.job.is_failed)

    @property
    def is_finished(self) -> bool:
        return cast(bool,self.job.is_finished)
    
    @property
    def is_paused(self) -> bool:
        return cast(bool,self.meta.get("paused",False))

    @property
    def id(self) -> str:
        return cast(str,self.job.id)

    @property
    def kwargs(self) -> typing.Dict:
        return cast(dict,self.job.kwargs)
    
    @property
    def result(self) -> Any:
        return self.job.result
    
    @property
    def created_at(self) -> datetime:
        return cast(datetime,self.job.created_at)
    
    @property
    def enqueued_at(self) -> datetime:
        return cast(datetime,self.job.enqueued_at)

    @classmethod
    def get_all_jobs(cls, type:str="batch") -> Generator['WrappedJob', None, None]:
        """
        Get all jobs of a given type from the queue
        """
        registries = [
            rq_slow_queue.started_job_registry,     # Returns StartedJobRegistry
            rq_slow_queue.deferred_job_registry,    # Returns DeferredJobRegistry
            rq_slow_queue.finished_job_registry,    # Returns FinishedJobRegistry
            rq_slow_queue.failed_job_registry,      # Returns FailedJobRegistry 
            rq_slow_queue.scheduled_job_registry,   # Returns ScheduledJobRegistry
            rq_slow_queue.canceled_job_registry,    # Returns CanceledJobRegistry
        ]
        job_ids = set()
        for registry in registries:
            for j_id in registry.get_job_ids():
                job_ids.add(j_id)
        for j_id in rq_slow_queue.job_ids:
            job_ids.add(j_id)
        jobs = (Job.fetch(j_id) for j_id in job_ids)

        return (WrappedJob(j) for j in jobs if j and j.get_meta().get("type") == type)

def _is_offpeak(offpeak_start: str, offpeak_end: str, current_time) -> bool:
    try:
        start_time = datetime.strptime(offpeak_start, "%H:%M").time()
        end_time = datetime.strptime(offpeak_end, "%H:%M").time()
    except Exception as e:
        logger.error(f"Unable to parse offpeak time: {offpeak_start}, {offpeak_end}", None)  # handle_error
        return True

    if start_time < end_time:
        return bool(current_time >= start_time and current_time <= end_time)
    # End time is after midnight
    return bool(current_time >= start_time or current_time <= end_time)
