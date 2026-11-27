"""Explicit backend selection; the Kubernetes process never imports Docker startup."""
import importlib
import os


def load_app(backend=None):
    backend = os.environ.get('PERSONAL_CLOUD_RUNTIME_BACKEND', 'docker') if backend is None else backend
    if backend == 'docker':
        return importlib.import_module('manager').app
    if backend == 'kubernetes':
        return importlib.import_module('manager_kubernetes').build_from_env()
    raise RuntimeError('PERSONAL_CLOUD_RUNTIME_BACKEND_INVALID')


app = load_app()
