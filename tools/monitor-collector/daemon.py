"""Local monitor collector loop candidate. No service installation or restart."""
import argparse
import json
import os
import signal
import sys
import threading
import time

from cache import collector_lock, publish_cache
from collector import Collector
from storage import StorageCache


def run(directory, interval=15, stop=None, collector=None):
    if os.geteuid() != 0 or not 5 <= interval <= 120: raise ValueError('COLLECTOR_POLICY')
    stop = stop or threading.Event(); collector = collector or Collector(storage=StorageCache())
    lock = collector_lock(directory)
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                sample = collector.sample(budget_seconds=min(10, max(1, interval - 1)))
                publish_cache(sample, directory)
            except Exception:
                # Fixed operational event only: never stringify raw Docker/path errors.
                print(json.dumps({'schema':'maimi-monitor-collector-status-v1','status':'SAMPLE_NOT_PUBLISHED'}), flush=True)
            stop.wait(max(1, interval - (time.monotonic() - started)))
    finally: os.close(lock)


def main():
    parser = argparse.ArgumentParser(description='Publish bounded monitoring metadata to a provisioned private directory.')
    parser.add_argument('--cache-directory', required=True)
    parser.add_argument('--interval-seconds', type=int, default=15)
    args = parser.parse_args(); stop = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM): signal.signal(number, lambda *_: stop.set())
    run(args.cache_directory, args.interval_seconds, stop=stop)


if __name__=='__main__':
    sys.dont_write_bytecode=True
    main()
