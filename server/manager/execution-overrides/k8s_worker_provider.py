"""Kubernetes personal-worker lifecycle candidate. No import-time IO or credentials.

This is not an installer or an activated replacement for the frozen r65 manager.
Only trusted manager code constructs Config/WorkerSpec. Chat input is not accepted.
Python 3.10+, standard library. Namespace RBAC and runtime qualification are external.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import ssl
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import InitVar, dataclass, replace
from execution_pool import PROFILES, ExecutionPool


PREFIX = "maimi.megmeet.com/"
MAX_RESPONSE = 2 * 1024 * 1024
IMAGE_PATTERN = r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}"


class ProviderError(Exception):
    """Fixed diagnostic; never include API body, credentials or caller values."""


class OutcomeUnknown(ProviderError):
    pass


def require(condition, code="INVALID_CONFIGURATION"):
    if not condition:
        raise ProviderError(code)


def label(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value) is not None


def uid(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value) is not None


def temporary_turn_owner(owner):
    return isinstance(owner,str) and re.fullmatch(r'f7a9[0-9a-f]{8}8[0-9a-f]{3}[89ab][0-9a-f]{15}',owner) is not None


@dataclass(frozen=True)
class ImageAdmission:
    worker_image: str
    card_image: str
    release_sha256: str
    runtime: str = "0.2.1-rc.3"
    files_image: str = ""

    def __post_init__(self):
        require(self.runtime in ("0.2.1-rc.3", "0.2.1-rc.5", "0.2.2-rc.1", "0.2.2-rc.2", "0.2.2-rc.3", "0.2.2-rc.5"), "RUNTIME_ADMISSION_REFUSED")
        require(all(isinstance(v, str) and re.fullmatch(IMAGE_PATTERN, v) for v in
                    (self.worker_image, self.card_image)), "IMAGE_ADMISSION_REFUSED")
        require(isinstance(self.release_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", self.release_sha256), "IMAGE_ADMISSION_REFUSED")
        require(not self.files_image or isinstance(self.files_image,str) and re.fullmatch(IMAGE_PATTERN,self.files_image), "FILES_IMAGE_ADMISSION_REFUSED")

    @classmethod
    def load(cls, path, pinned_sha256):
        """Read an independently pinned release file, never a Docker config ID."""
        require(isinstance(path, str) and os.path.isabs(path), "IMAGE_ADMISSION_REFUSED")
        require(isinstance(pinned_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", pinned_sha256), "IMAGE_ADMISSION_REFUSED")
        try:
            with open(path, "rb") as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022 and
                        0 < info.st_size <= 16384, "IMAGE_ADMISSION_FILE_REFUSED")
                raw = stream.read(16385)
            require(hashlib.sha256(raw).hexdigest() == pinned_sha256, "IMAGE_ADMISSION_SHA_REFUSED")
            value = json.loads(raw)
            require(isinstance(value, dict) and value.get("schema") in ("maimi-k8s-worker-image-admission/v1","maimi-k8s-worker-image-admission/v2"), "IMAGE_ADMISSION_REFUSED")
            files_image=value.get('files_image','') if value['schema'].endswith('/v2') else ''
            require(not value['schema'].endswith('/v2') or bool(files_image),'FILES_IMAGE_ADMISSION_REFUSED')
            return cls(value["worker_image"], value["card_image"], pinned_sha256, value["runtime"],files_image)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("IMAGE_ADMISSION_UNAVAILABLE") from None


@dataclass(frozen=True)
class Config:
    namespace: str
    instance: str
    image: str
    state_storage_class: str
    files_storage_class: str
    worker_node: str
    model_base_url: str
    image_admission: ImageAdmission
    runtime_class: str = ""
    state_access_mode: str = "ReadWriteOnce"
    files_access_mode: str = "ReadWriteMany"
    state_gib: int = 2
    files_gib: int = 5
    cpu_millis: int = 1000
    memory_mib: int = 1536
    ephemeral_mib: int = 1024
    execution_pool: ExecutionPool | None = None
    pool_role: str = 'execution'
    shared_files_prefix: str = ''
    shared_nfs_server: str = ''
    shared_nfs_export: str = ''
    shared_files_backend: str = 'nfs'
    shared_local_node_uid: str = ''
    workspace_state_backend: str = ''
    relocatable_chat_state: bool = False
    local_state_root: str = ''

    def __post_init__(self):
        require(self.pool_role in ('execution','chat'),'POOL_ROLE_INVALID')
        require(type(self.relocatable_chat_state) is bool,'RELOCATABLE_STATE_FLAG_INVALID')
        if self.relocatable_chat_state:
            require(self.pool_role=='chat' and self.workspace_state_backend=='postgres' and self.shared_files_backend=='nfs' and bool(self.shared_files_prefix) and self.execution_pool is not None,'RELOCATABLE_STATE_SCOPE_REFUSED')
            require(self.state_storage_class=='maimi-prod-local-state-retain' and re.fullmatch(r'/data/[a-zA-Z0-9_/-]{1,200}',self.local_state_root) and '..' not in self.local_state_root,'LOCAL_STATE_ROOT_REQUIRED')
        require(self.workspace_state_backend in ('','postgres'),'WORKSPACE_STATE_BACKEND_INVALID')
        require(not self.workspace_state_backend or (self.pool_role=='chat' and self.image_admission.runtime in ('0.2.1-rc.5','0.2.2-rc.1','0.2.2-rc.2','0.2.2-rc.3','0.2.2-rc.5')),'WORKSPACE_STATE_BACKEND_SCOPE_REFUSED')
        require(self.shared_files_backend in ('nfs','single-node-local-test'),'SHARED_BACKEND_INVALID')
        local_test = self.shared_files_backend == 'single-node-local-test'
        if local_test:
            from local_files_contract import NODE, NODE_UID, STORAGE_CLASS, INSTANCES
            require(self.namespace in INSTANCES and self.instance==INSTANCES[self.namespace] and
                    self.pool_role==('chat' if self.namespace=='maimi-r78-app' else 'execution') and
                    self.worker_node==NODE and self.shared_local_node_uid==NODE_UID and
                    self.files_storage_class==STORAGE_CLASS and self.files_access_mode=='ReadWriteOnce' and
                    self.state_storage_class=='maimi-test-state-retain' and self.runtime_class=='maimi-runsc' and
                    self.shared_files_prefix=='owners' and not self.shared_nfs_server and not self.shared_nfs_export and
                    self.files_gib==5, 'LOCAL_TEST_STORAGE_CONFIGURATION_REFUSED')
        else:
            require(not self.shared_local_node_uid,'LOCAL_TEST_STORAGE_CONFIGURATION_REFUSED')
        if self.shared_files_prefix:
            require(re.fullmatch(r'[a-z0-9][a-z0-9/-]{0,127}',self.shared_files_prefix) and '..' not in self.shared_files_prefix,
                    'SHARED_FILES_PREFIX_INVALID')
            require(local_test or re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}',self.shared_nfs_server) and
                    re.fullmatch(r'/[a-zA-Z0-9_/-]{1,255}',self.shared_nfs_export) and '..' not in self.shared_nfs_export,
                    'SHARED_NFS_CONFIGURATION_REQUIRED')
        require(all(label(x) for x in (self.namespace, self.instance, self.state_storage_class, self.files_storage_class)))
        require(isinstance(self.worker_node, str) and 0 < len(self.worker_node) <= 253 and
                all(label(part) for part in self.worker_node.split(".")), "WORKER_NODE_REQUIRED")
        require(not self.runtime_class or label(self.runtime_class))
        require(isinstance(self.image_admission, ImageAdmission) and self.image == self.image_admission.worker_image,
                "IMAGE_NOT_IN_REVIEWED_RELEASE")
        require(self.state_access_mode in ("ReadWriteOnce", "ReadWriteOncePod"), "STATE_ACCESS_MODE_REFUSED")
        require(self.files_access_mode in ("ReadWriteMany", "ReadWriteOnce", "ReadWriteOncePod"), "FILES_ACCESS_MODE_REFUSED")
        require(local_test or not self.shared_files_prefix or self.files_access_mode=='ReadWriteMany','SHARED_FILES_REQUIRE_RWX')
        # Exact current worker bootstrap contract, narrowed to a valid same-namespace
        # Kubernetes Service name. The frozen worker rejects FQDN and HTTPS here.
        model = re.fullmatch(r"http://([a-z0-9-]+):8080/internal/personal-cloud/model/v1", self.model_base_url) if isinstance(self.model_base_url, str) else None
        require(model is not None and label(model.group(1)), "WORKER_MODEL_BASE_CONTRACT_REFUSED")
        for value, low, high in ((self.state_gib, 1, 1024), (self.files_gib, 1, 1024),
                                 (self.cpu_millis, 100, 8000), (self.memory_mib, 256, 16384),
                                 (self.ephemeral_mib, 128, 16384)):
            require(type(value) is int and low <= value <= high)


@dataclass(frozen=True)
class WorkerSpec:
    owner: str
    generation: int
    catalog_chunks: int = 1
    mode: str = "worker"
    empty_storage: bool = False
    resource_class: str = 'legacy'
    node_name: str = ''
    network_mode: str | None = None

    def __post_init__(self):
        require(isinstance(self.owner, str) and re.fullmatch(r"[0-9a-f]{32}", self.owner) is not None, "INVALID_WORKER_SPEC")
        require(type(self.generation) is int and 1 <= self.generation <= 2147483647, "INVALID_WORKER_SPEC")
        require(type(self.catalog_chunks) is int and 1 <= self.catalog_chunks <= 9, "INVALID_WORKER_SPEC")
        require(self.network_mode in (None, 'public', 'confidential') and (self.mode == 'worker' or self.network_mode is None), 'INVALID_NETWORK_MODE')
        require(self.mode in ("worker", "card", "files") and type(self.empty_storage) is bool, "INVALID_WORKER_SPEC")
        require(self.mode == "worker" or not self.empty_storage, "MAINTENANCE_REQUIRES_ADMITTED_STORAGE")
        require(self.resource_class in ('legacy',*PROFILES),'INVALID_RESOURCE_CLASS')
        require(not self.node_name or re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}',self.node_name),'INVALID_NODE_NAME')

    @property
    def name(self):
        return "mmw-" + self.owner + "-g" + str(self.generation)

    @property
    def secret_name(self):
        return self.name + "-bootstrap"


@dataclass(frozen=True)
class Handle:
    namespace: str
    instance: str
    owner: str
    generation: int
    name: str
    pod_uid: str
    service_uid: str
    catalog_chunks: int = 1
    node_name: str = ""
    secret_uid: str = ""
    state_pvc_uid: str = ""
    files_pvc_uid: str = ""
    mode: str = "worker"
    empty_storage: bool = False
    resource_class: str = 'legacy'
    network_mode: str | None = None
    image_pin: InitVar[str | None] = None

    def __post_init__(self, image_pin):
        require(image_pin is None or isinstance(image_pin, str) and re.fullmatch(IMAGE_PATTERN, image_pin),
                "BOUND_IMAGE_REFUSED")
        # This auxiliary value is persisted separately from the legacy termination-proof identity.
        object.__setattr__(self, "bound_image", image_pin)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ProviderError("API_REDIRECT_REFUSED")


class KubernetesHTTPS:
    """Explicit HTTPS endpoint/CA/token callback; no env proxy or kubeconfig lookup.

    token_loader is a trusted server-side secret-file reader supplied by the manager.
    It is called per request to support rotation. No tokens are stored in manifests.
    No retry: a failed mutation has an unknown outcome, requiring reconciliation.
    """
    def __init__(self, endpoint, ca_file, token_loader, namespace):
        require(label(namespace))
        self.namespace = namespace
        try:
            url = urllib.parse.urlsplit(endpoint)
            require(url.scheme == "https" and url.hostname and url.path in ("", "/") and
                    not url.username and not url.password and not url.query and not url.fragment)
            context = ssl.create_default_context(cafile=ca_file)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                NoRedirect(), urllib.request.HTTPSHandler(context=context))
        except (OSError, ValueError, TypeError, ssl.SSLError):
            raise ProviderError("API_CONFIGURATION_REFUSED") from None
        self.endpoint = endpoint.rstrip("/")
        require(callable(token_loader))
        self.token_loader = token_loader

    async def request(self, method, path, body=None):
        return await asyncio.to_thread(self._request, method, path, body)

    def validate_path(self, method, path):
        require(method in ("GET", "POST", "DELETE") and isinstance(path, str) and len(path) < 4096, "API_REQUEST_REFUSED")
        parsed = urllib.parse.urlsplit(path)
        require(not parsed.scheme and not parsed.netloc and not parsed.fragment and
                "%" not in parsed.path and ".." not in parsed.path and "\\" not in parsed.path, "API_REQUEST_REFUSED")
        core = "/api/v1/namespaces/" + self.namespace + "/"
        discovery = "/apis/discovery.k8s.io/v1/namespaces/" + self.namespace + "/endpointslices"
        if parsed.path == discovery:
            require(method == "GET", "API_REQUEST_REFUSED")
        elif parsed.path.startswith('/api/v1/persistentvolumes/'):
            require(method=='GET' and label(parsed.path.removeprefix('/api/v1/persistentvolumes/')),'API_REQUEST_REFUSED')
        elif parsed.path in ('/api/v1/nodes/maimi-r69-test',
                             '/apis/storage.k8s.io/v1/storageclasses/maimi-test-shared-local-retain',
                             '/apis/storage.k8s.io/v1/storageclasses/maimi-prod-local-state-retain'):
            require(method=='GET','API_REQUEST_REFUSED')
        else:
            require(parsed.path.startswith(core), "API_REQUEST_REFUSED")
            parts = parsed.path[len(core):].split("/")
            require(len(parts) in (1, 2) and parts[0] in ("pods", "services", "persistentvolumeclaims", "secrets"), "API_REQUEST_REFUSED")
            if len(parts) == 2:
                require(label(parts[1]) and method in ("GET", "DELETE"), "API_REQUEST_REFUSED")
            else:
                require(method == "POST" or method == "GET" and parts[0] == "pods", "API_REQUEST_REFUSED")
        try:
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise ProviderError("API_REQUEST_REFUSED") from None
        require(not query or method == "GET" and parsed.path in (core + "pods", discovery), "API_REQUEST_REFUSED")
        require(set(query) <= {"limit", "labelSelector"} and all(len(v) == 1 for v in query.values()), "API_REQUEST_REFUSED")
        if "limit" in query:
            require(query["limit"] == ["200"], "API_REQUEST_REFUSED")

    def _request(self, method, path, body):
        self.validate_path(method, path)
        try:
            token = self.token_loader()
            require(isinstance(token, str) and 0 < len(token) <= 32768 and not any(c.isspace() for c in token), "API_CREDENTIAL_UNAVAILABLE")
            data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
            request = urllib.request.Request(self.endpoint + path, data=data, method=method,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Content-Type": "application/json"})
            try:
                response = self.opener.open(request, timeout=15)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.code
                raw = response.read(MAX_RESPONSE + 1)
            require(len(raw) <= MAX_RESPONSE, "API_RESPONSE_REFUSED")
            # Error bodies may include admission diagnostics or user data; discard.
            if status not in (200, 201, 202):
                return status, None
            result = json.loads(raw)
            require(isinstance(result, dict), "API_RESPONSE_REFUSED")
            return status, result
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("API_TRANSPORT_FAILED") from None


def subset(expected, actual):
    """Accept API-added defaults, but never changed requested policy or extra list items."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(subset(a, b) for a, b in zip(expected, actual))
    return type(expected) is type(actual) and expected == actual


def binary_storage_equal(expected, actual):
    """Strict bounded BinarySI integers only, scoped to ephemeral-storage."""
    def size(value):
        if not isinstance(value, str) or len(value) > 32:
            return None
        match = re.fullmatch(r'(0|[1-9][0-9]{0,17})(Ki|Mi|Gi|Ti|Pi|Ei)', value)
        if not match:
            return None
        return int(match[1]) * (1024 ** (('Ki','Mi','Gi','Ti','Pi','Ei').index(match[2]) + 1))
    left, right = size(expected), size(actual)
    return left is not None and right is not None and left == right


class KubernetesWorkerProvider:
    def __init__(self, config: Config, api, worker_http=None):
        require(isinstance(config, Config) and callable(getattr(api, "request", None)))
        self.config, self.api = config, api
        self.worker_http = worker_http
        self.base = "/api/v1/namespaces/" + config.namespace

    def labels(self, spec, generation=True):
        result = {PREFIX + "instance": self.config.instance, PREFIX + "owner": spec.owner}
        if generation:
            result[PREFIX + "generation"] = str(spec.generation)
            result[PREFIX + "mode"] = spec.mode
            if self.config.execution_pool and spec.mode=='worker':
                result[PREFIX+'component']='chat-host' if self.config.pool_role=='chat' else 'execution-worker'
        return result

    def state_claim_name(self,spec):
        from state_placement import state_name
        return state_name(spec.owner,spec.node_name or self.config.worker_node) if self.config.relocatable_chat_state else 'mmw-'+spec.owner+'-state'

    def admitted_image(self, spec):
        require(isinstance(spec, WorkerSpec), "INVALID_WORKER_SPEC")
        cfg = self.config
        image = cfg.image if spec.mode == "worker" else cfg.image_admission.card_image if spec.mode == "card" else cfg.image_admission.files_image
        require(isinstance(image, str) and re.fullmatch(IMAGE_PATTERN, image), "IMAGE_ADMISSION_REFUSED")
        return image

    def manifests(self, spec: WorkerSpec):
        return self._manifests(spec, self.admitted_image(spec))

    def bound_manifests(self, spec, handle):
        image = handle.bound_image if handle.bound_image is not None else self.admitted_image(spec)
        return self._manifests(spec, image)

    def _manifests(self, spec, image):
        require(isinstance(spec, WorkerSpec), "INVALID_WORKER_SPEC")
        cfg = self.config
        temporary=temporary_turn_owner(spec.owner)
        if temporary:
            require(cfg.pool_role=='execution' and cfg.execution_pool and spec.mode=='worker' and
                    spec.network_mode=='confidential' and spec.empty_storage,'TEMPORARY_VOLUME_SCOPE_REFUSED')
        resource = None
        if cfg.execution_pool:
            cfg.execution_pool.node(spec.node_name)
            require(spec.resource_class in PROFILES,'EXECUTION_PROFILE_REQUIRED')
            resource=PROFILES[spec.resource_class]
            cfg=replace(cfg,worker_node=spec.node_name,cpu_millis=resource.cpu,
                        memory_mib=resource.memory,ephemeral_mib=resource.ephemeral)
        labels = self.labels(spec)
        meta = {"name": spec.name, "namespace": cfg.namespace, "labels": labels}
        env = [{"name": "CLOUD_OWNER_KEY", "value": spec.owner},
               {"name": "CLOUD_WORKER_GENERATION", "value": str(spec.generation)},
               {"name": "CLOUD_POD_UID", "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}}}]
        names = ["CLOUD_ROUTE_TOKEN"]
        if temporary:
            env.append({'name':'MAIMI_TEMPORARY_TURN','value':'1'})
        if cfg.relocatable_chat_state and spec.mode=="worker":
            env += [{"name":"MAIMI_RELOCATABLE_STATE","value":"1"},{"name":"MAIMI_STATE_FIRST_USE","value":"1" if spec.empty_storage else "0"}]
        if spec.mode == "worker" and cfg.workspace_state_backend:
            env.append({"name":"MAIMI_WORKSPACE_STATE_BACKEND","value":cfg.workspace_state_backend})
        if spec.mode == "worker" and cfg.execution_pool and cfg.pool_role=='execution':
            env += [{'name':'CLOUD_RESOURCE_CLASS','value':spec.resource_class},
                    {'name':'CLOUD_STATE_ROOT','value':'/state'},
                    {'name':'CLOUD_WORKSPACE_ROOT','value':'/workspace'}]
        elif spec.mode == "worker":
            env.append({"name": "CLOUD_MODEL_BASE_URL", "value": cfg.model_base_url})
            names += ["CLOUD_MODEL_LEASE", "CLOUD_MODEL_CATALOG_COUNT"]
            names += ["CLOUD_MODEL_CATALOG_" + str(i) for i in range(spec.catalog_chunks)]
        else:
            env.append({"name": "CLOUD_MAINTENANCE", "value": "card-retention-v1" if spec.mode=='card' else 'source-retention-v1'})
        env += [{"name": name, "valueFrom": {"secretKeyRef": {"name": spec.secret_name, "key": name, "optional": False}}} for name in names]
        volume_names = ("state", "files") if spec.mode == "worker" else ("state",) if spec.mode=='card' else ('files',)
        mounts = [{"name": x, "mountPath": "/state" if x == "state" else "/workspace"} for x in volume_names]
        mounts.append({"name": "tmp", "mountPath": "/tmp"})
        volumes = [{"name": x, "persistentVolumeClaim": {"claimName": self.state_claim_name(spec) if x=="state" else "mmw-" + spec.owner + "-files"}} for x in volume_names]
        if temporary:
            # Both roots belong to this Pod UID. No employee or NAS claim is mounted.
            size=min(1024,cfg.ephemeral_mib-96)
            volumes=[{'name':'state','emptyDir':{'sizeLimit':'64Mi'}},
                     {'name':'files','emptyDir':{'sizeLimit':'1Gi' if size==1024 else str(size)+'Mi'}}]
        # 128 MiB cache + two bounded 32 MiB inputs + image conversion headroom;
        # memory-backed emptyDir remains charged inside the 1536 MiB Pod budget.
        volumes.append({"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "256Mi"}})
        # resource.Quantity serializes to canonical SI/BinarySI, e.g. 1000m->1.
        cpu = str(cfg.cpu_millis // 1000) if cfg.cpu_millis % 1000 == 0 else str(cfg.cpu_millis) + "m"
        memory = str(cfg.memory_mib // 1024) + "Gi" if cfg.memory_mib % 1024 == 0 else str(cfg.memory_mib) + "Mi"
        # Kubelet enforcement depends on supported filesystem accounting; this
        # declaration is not a PVC quota or a per-Pod process-count limit.
        resources = {"cpu": cpu, "memory": memory, "ephemeral-storage": str(cfg.ephemeral_mib) + "Mi"}
        pod_spec = {"automountServiceAccountToken": False, "enableServiceLinks": False,
            "restartPolicy": "Never", "terminationGracePeriodSeconds": 30,
            "hostNetwork": False, "hostPID": False, "hostIPC": False,
            "nodeSelector": {"kubernetes.io/hostname": cfg.worker_node},
            "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
                {"matchFields": [{"key": "metadata.name", "operator": "In", "values": [cfg.worker_node]}]}]}}},
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000, "fsGroupChangePolicy": "OnRootMismatch",
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "worker", "image": image, "imagePullPolicy": "IfNotPresent", "workingDir": "/state" if spec.mode == "card" else "/workspace",
                "command": ["node", "/opt/personal-cloud/"+('execution-agent.mjs' if cfg.execution_pool and cfg.pool_role=='execution' else 'worker.mjs')] if spec.mode == "worker" else ["python3", "-I", "-B", "/opt/personal-cloud/"+('card_retention_server.py' if spec.mode=='card' else 'source_retention_server.py')],
                "ports": [{"name": "worker", "containerPort": 3084}], "env": env, "volumeMounts": mounts,
                "resources": {"requests": resources, "limits": resources},
                "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                    "privileged": False, "runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                    "seccompProfile": {"type": "RuntimeDefault"}, "capabilities": {"drop": ["ALL"]}},
                # TCP is only a Kubernetes routing prerequisite, not native readiness.
                "readinessProbe": {"tcpSocket": {"port": "worker"}, "periodSeconds": 2, "failureThreshold": 3}}],
            "volumes": volumes}
        if resource:
            pod_spec['containers'][0]['resources']['limits'] = dict(resources,cpu=str(resource.cpu_limit//1000))
        if cfg.runtime_class:
            pod_spec["runtimeClassName"] = cfg.runtime_class
        if cfg.shared_files_backend=='single-node-local-test':
            # The provisioners already set 1000:1000. Do not recursively change
            # the shared workspace when the second role mounts it.
            pod_spec['securityContext'].pop('fsGroup')
            pod_spec['containers'][0]['imagePullPolicy']='Never'
        if spec.empty_storage:
            # A newly allocated PVC may bind a recycled PV. Verify empty roots
            # inside the actual mounts before native DSH may write either volume.
            roots="('/state',)" if cfg.shared_files_prefix and not temporary else "('/state','/workspace')"
            script = "import os,sys; sys.exit(0 if all(not os.listdir(p) for p in "+roots+") else 42)"
            pod_spec["initContainers"] = [{"name": "empty-volume-admission", "image": image,
                "imagePullPolicy": "IfNotPresent", "command": ["python3", "-I", "-B", "-c", script],
                "volumeMounts": [{**x, "readOnly": True} for x in mounts if x["name"] != "tmp"],
                "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
                "securityContext": pod_spec["containers"][0]["securityContext"]}]
            if cfg.shared_files_backend=='single-node-local-test':
                pod_spec['initContainers'][0]['imagePullPolicy']='Never'
        digest = hashlib.sha256(json.dumps(pod_spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {**meta, "annotations": {PREFIX + "spec-sha256": digest}}, "spec": pod_spec}
        if spec.network_mode is not None:
            pod["metadata"]["labels"] = {**labels, PREFIX + "network-mode": spec.network_mode}
        if temporary:
            pod["metadata"]["labels"][PREFIX+'storage-lifecycle']='temporary-turn'
        service = {"apiVersion": "v1", "kind": "Service", "metadata": meta,
            "spec": {"type": "ClusterIP", "selector": labels, "ports": [{"name": "worker", "port": 3084, "targetPort": "worker", "protocol": "TCP"}]}}
        pvcs = []
        if temporary:
            return {"pod":pod,"service":service,"pvcs":pvcs}
        for name, size, storage_class, access in (("state", cfg.state_gib, cfg.state_storage_class, cfg.state_access_mode),
                                                ("files", cfg.files_gib, cfg.files_storage_class, cfg.files_access_mode)):
            pvcs.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": self.state_claim_name(spec) if name=="state" else "mmw-" + spec.owner + "-files", "namespace": cfg.namespace, "labels": self.labels(spec, False)},
                "spec": {"accessModes": [access], "volumeMode": "Filesystem", "storageClassName": storage_class,
                         "resources": {"requests": {"storage": str(size // 1024) + "Ti" if size % 1024 == 0 else str(size) + "Gi"}}}})
            if name=='state' and cfg.relocatable_chat_state:
                pvcs[-1]['metadata']['annotations']={PREFIX+'state-node':spec.node_name or cfg.worker_node,PREFIX+'state-layout':'pg-workspace-node-local-v1'}
            if name=='files' and cfg.shared_files_prefix:
                if cfg.shared_files_backend=='single-node-local-test':
                    from local_files_contract import annotations
                    pvcs[-1]['metadata']['annotations']=annotations(spec.owner)
                else:
                    pvcs[-1]['metadata']['annotations']={'nfs.io/storage-path':cfg.shared_files_prefix+'/'+spec.owner+'/workspace'}
        return {"pod": pod, "service": service, "pvcs": pvcs}

    async def call(self, method, path, body=None):
        try:
            status, result = await self.api.request(method, path, body)
            require(type(status) is int, "API_RESPONSE_REFUSED")
            return status, result
        except Exception:
            error = OutcomeUnknown if method != "GET" else ProviderError
            raise error("API_MUTATION_OUTCOME_UNKNOWN" if method != "GET" else "API_READ_FAILED") from None

    def verify(self, expected, observed, terminating=False):
        require(isinstance(observed, dict), "RESOURCE_IDENTITY_REFUSED")
        meta = observed.get("metadata", {})
        require(isinstance(meta, dict) and uid(meta.get("uid")) and (terminating or not meta.get("deletionTimestamp")), "RESOURCE_IDENTITY_REFUSED")
        comparable = observed
        if expected["kind"] == "Pod" and isinstance(observed.get("spec"), dict):
            # These three concrete bool fields are json omitempty in core/v1.
            # False may therefore disappear on a successful API round trip.
            comparable = {**observed, "spec": {**observed["spec"]}}
            for key in ("hostNetwork", "hostPID", "hostIPC"):
                comparable["spec"].setdefault(key, False)
            # The API canonicalizes 1024Mi to 1Gi. Compare only this exact
            # resource field by integer bytes; do not relax labels, annotations,
            # resource values, other fields, or the original spec hash.
            containers = []
            for requested, found in zip(expected["spec"].get("containers", []), observed["spec"].get("containers", [])):
                found = dict(found)
                resources = dict(found.get("resources", {}))
                for scope in ("requests", "limits"):
                    values = dict(resources.get(scope, {}))
                    wanted = requested.get("resources", {}).get(scope, {}).get("ephemeral-storage")
                    actual = values.get("ephemeral-storage")
                    if binary_storage_equal(wanted, actual):
                        values["ephemeral-storage"] = wanted
                    resources[scope] = values
                found["resources"] = resources
                containers.append(found)
            # Never truncate an unexpected sidecar during normalization.
            if len(containers) == len(observed["spec"].get("containers", [])):
                comparable["spec"]["containers"] = containers
        require(subset(expected, comparable), "RESOURCE_POLICY_OR_OWNERSHIP_REFUSED")
        # Persistent claims must not be garbage collected with an execution Pod.
        if expected["kind"] in ("PersistentVolumeClaim", "Pod", "Service"):
            require(not meta.get("ownerReferences"), "PVC_RETENTION_REFUSED")
        if expected["kind"] == "Pod":
            key = PREFIX + 'network-mode'
            require(meta.get('labels', {}).get(key) == expected['metadata']['labels'].get(key), 'POD_NETWORK_MODE_CHANGED')
            spec = observed["spec"]
            require(not spec.get("ephemeralContainers") and ("initContainers" in expected["spec"] or not spec.get("initContainers")), "POD_EXTRA_CONTAINER_REFUSED")
            expected_node=expected['spec']['nodeSelector']['kubernetes.io/hostname']
            require(not spec.get("nodeName") or spec["nodeName"] == expected_node, "WORKER_NODE_CHANGED")
            if not self.config.runtime_class:
                require(not spec.get("runtimeClassName"), "RUNTIME_CLASS_CHANGED")
            for container in spec["containers"]:
                require(not container.get("envFrom") and not container.get("lifecycle") and not container.get("args"), "POD_EXTRA_BEHAVIOR_REFUSED")
        if expected["kind"] == "Service":
            require(not observed["spec"].get("externalIPs") and not observed["spec"].get("externalName") and
                    not observed["spec"].get("publishNotReadyAddresses") and
                    all(not p.get("nodePort") for p in observed["spec"].get("ports", [])), "SERVICE_EXPOSURE_REFUSED")
        return meta["uid"]

    async def ensure(self, plural, desired):
        path = self.base + "/" + plural
        status, observed = await self.call("POST", path, desired)
        if status == 409:
            status, observed = await self.call("GET", path + "/" + desired["metadata"]["name"])
        require(status in (200, 201), "RESOURCE_CREATE_REFUSED")
        return self.verify(desired, observed)

    async def ensure_owner_storage(self, spec, expected_uids=None):
        objects = self.manifests(spec)
        if temporary_turn_owner(spec.owner):
            require(not objects['pvcs'] and expected_uids in (None,{'state':None,'files':None}),
                    'TEMPORARY_VOLUME_BINDING_REFUSED')
            return {'state':None,'files':None}
        require(expected_uids is None or isinstance(expected_uids, dict) and set(expected_uids) == {"state", "files"} and
                all(uid(v) for v in expected_uids.values()), "PVC_BINDING_REQUIRED")
        require(expected_uids is not None or spec.mode == "worker" and spec.empty_storage, "FRESH_OWNER_EMPTY_ADMISSION_REQUIRED")
        result = {}
        for role, claim in zip(("state", "files"), objects["pvcs"]):
            path = self.base + "/persistentvolumeclaims/" + claim["metadata"]["name"]
            scope_key = PREFIX + "network-mode"
            if expected_uids is None:
                if spec.network_mode is not None:
                    claim["metadata"]["labels"][scope_key] = spec.network_mode
                status, observed = await self.call("POST", self.base + "/persistentvolumeclaims", claim)
                require(status == 201, "UNBOUND_EXISTING_PVC_REFUSED")
            else:
                status, observed = await self.call("GET", path)
                require(status == 200, "BOUND_PVC_MISSING")
            # Previously admitted volumes belong to the original, ordinary workspace.
            # Maintenance pods are offline and operate only on previously bound volume UIDs.
            if spec.mode == 'worker':
                observed_scope = observed.get('metadata', {}).get('labels', {}).get(scope_key, 'public')
                require(observed_scope == (spec.network_mode or 'public'), 'PVC_NETWORK_MODE_MISMATCH')
            result[role] = self.verify(claim, observed)
            require(expected_uids is None or result[role] == expected_uids[role], "PVC_UID_CHANGED")
            if role=='state' and self.config.relocatable_chat_state:
                from state_placement import verify_local_state
                await verify_local_state(self,spec,observed,result[role],require_bound=False)
            if role=='files' and self.config.shared_files_prefix:
                # NFS classes must bind immediately. Bounded read-only polling
                # verifies the actual PV; an annotation alone grants no access.
                for _ in range(40):
                    if observed.get('spec',{}).get('volumeName') and (self.config.shared_files_backend!='single-node-local-test' or
                            observed.get('status',{}).get('phase')=='Bound'):
                        break
                    await asyncio.sleep(.25)
                    status,observed=await self.call('GET',path)
                    require(status==200 and self.verify(claim,observed)==result[role],'SHARED_PVC_CHANGED')
                await self.verify_shared_files(spec,observed,result[role])
        return result

    async def verify_shared_files(self,spec,pvc,pvc_uid):
        cfg=self.config
        if cfg.shared_files_backend=='single-node-local-test':
            from local_files_contract import claim,verify_node,verify_class,verify_pv,NODE_PATH,SC_PATH,LocalFilesError
            try:
                descriptor=claim(pvc)
                require(descriptor['uid']==pvc_uid and descriptor['owner']==spec.owner and
                        descriptor['namespace']==cfg.namespace and pvc.get('status',{}).get('phase')=='Bound',
                        'LOCAL_FILES_CLAIM_BINDING_CHANGED')
                status,node=await self.call('GET',NODE_PATH)
                require(status==200,'LOCAL_FILES_NODE_UNAVAILABLE');verify_node(node)
                status,storage_class=await self.call('GET',SC_PATH)
                require(status==200,'LOCAL_FILES_CLASS_UNAVAILABLE');verify_class(storage_class)
                status,pv=await self.call('GET','/api/v1/persistentvolumes/'+descriptor['pv_name'])
                require(status==200,'LOCAL_FILES_PV_UNAVAILABLE');verify_pv(pv,descriptor,bound=True)
                return
            except LocalFilesError as exc:
                raise ProviderError(str(exc)) from None
        name=pvc.get('spec',{}).get('volumeName','')
        require(label(name),'SHARED_FILES_PV_NOT_BOUND')
        status,pv=await self.call('GET','/api/v1/persistentvolumes/'+name)
        require(status==200 and isinstance(pv,dict),'SHARED_FILES_PV_UNAVAILABLE')
        p=pv.get('spec',{}); ref=p.get('claimRef',{})
        expected_path=cfg.shared_nfs_export.rstrip('/')+'/'+cfg.shared_files_prefix+'/'+spec.owner+'/workspace'
        require(ref.get('namespace')==cfg.namespace and ref.get('name')=='mmw-'+spec.owner+'-files' and ref.get('uid')==pvc_uid,
                'SHARED_FILES_CLAIM_BINDING_CHANGED')
        require(p.get('nfs',{}).get('server')==cfg.shared_nfs_server and p.get('nfs',{}).get('path')==expected_path and
                p.get('persistentVolumeReclaimPolicy')=='Retain' and 'ReadWriteMany' in p.get('accessModes',[]) and
                not pv.get('metadata',{}).get('deletionTimestamp'),'SHARED_FILES_NFS_PATH_REFUSED')

    async def create_worker(self, spec, secret_uid, storage_uids):
        require(uid(secret_uid), "BOOTSTRAP_UID_REQUIRED")
        await self.ensure_owner_storage(spec, storage_uids)
        status, secret = await self.call("GET", self.base + "/secrets/" + spec.secret_name)
        require(status == 200 and isinstance(secret, dict) and secret.get("metadata", {}).get("uid") == secret_uid and
                subset(self.labels(spec), secret.get("metadata", {}).get("labels", {})) and secret.get("immutable") is True and
                not secret.get("metadata", {}).get("deletionTimestamp"), "BOOTSTRAP_IDENTITY_REFUSED")
        objects = self.manifests(spec)
        pod_uid = await self.ensure("pods", objects["pod"])
        service_uid = await self.ensure("services", objects["service"])
        return Handle(self.config.namespace, self.config.instance, spec.owner, spec.generation,
                      spec.name, pod_uid, service_uid, spec.catalog_chunks, spec.node_name or self.config.worker_node, secret_uid,
                      storage_uids["state"], storage_uids["files"], spec.mode, spec.empty_storage,spec.resource_class,spec.network_mode,
                      image_pin=objects["pod"]["spec"]["containers"][0]["image"])

    def check_handle(self, handle):
        require(isinstance(handle, Handle), "HANDLE_REFUSED")
        spec = WorkerSpec(handle.owner, handle.generation, handle.catalog_chunks, handle.mode, handle.empty_storage,handle.resource_class,handle.node_name,handle.network_mode)
        require(handle.namespace == self.config.namespace and handle.instance == self.config.instance and
                handle.name == spec.name and (self.config.execution_pool is not None or handle.node_name == self.config.worker_node) and
                all(uid(v) for v in (handle.pod_uid, handle.service_uid, handle.secret_uid)), "HANDLE_REFUSED")
        if temporary_turn_owner(handle.owner):
            require(self.config.pool_role=='execution' and handle.mode=='worker' and handle.network_mode=='confidential'
                    and handle.empty_storage and handle.state_pvc_uid is None and handle.files_pvc_uid is None,
                    'TEMPORARY_VOLUME_BINDING_REFUSED')
        else:
            require(uid(handle.state_pvc_uid) and uid(handle.files_pvc_uid),'HANDLE_REFUSED')
        return spec

    async def inspect_worker(self, handle):
        from k8s_worker_proofs import endpoint_slice_proof
        spec = self.check_handle(handle)
        objects = self.bound_manifests(spec, handle)
        status, pod = await self.call("GET", self.base + "/pods/" + handle.name)
        if status == 404:
            return {"api_absent": True, "ready": False, "execution_gone": False, "origin": None}
        require(status == 200, "RESOURCE_READ_REFUSED")
        require(self.verify(objects["pod"], pod, terminating=True) == handle.pod_uid, "POD_UID_CHANGED")
        status, service = await self.call("GET", self.base + "/services/" + handle.name)
        require(status == 200 and self.verify(objects["service"], service, terminating=True) == handle.service_uid, "SERVICE_IDENTITY_REFUSED")
        state = pod.get("status", {})
        result = {"api_absent": False, "ready": False, "execution_gone": False, "origin": None}
        ready = (not pod["metadata"].get("deletionTimestamp") and not service["metadata"].get("deletionTimestamp") and
                 pod["spec"].get("nodeName") == handle.node_name and state.get("phase") == "Running" and
                 any(x.get("type") == "Ready" and x.get("status") == "True" for x in state.get("conditions", [])))
        if not ready:
            return result
        if self.config.relocatable_chat_state:
            from state_placement import verify_local_state
            status,claim=await self.call("GET",self.base+"/persistentvolumeclaims/"+self.state_claim_name(spec))
            require(status==200,"LOCAL_STATE_CLAIM_UNAVAILABLE")
            await verify_local_state(self,spec,claim,handle.state_pvc_uid,require_bound=True)
        selector = urllib.parse.quote("kubernetes.io/service-name=" + handle.name, safe="")
        path = "/apis/discovery.k8s.io/v1/namespaces/" + handle.namespace + "/endpointslices?limit=200&labelSelector=" + selector
        status, slices = await self.call("GET", path)
        require(status == 200, "ENDPOINT_READ_REFUSED")
        proof = endpoint_slice_proof(handle, slices, state.get("podIP"))
        if not proof:
            return result
        # Gateway's existing origin validator permits a short service label only;
        # Gateway, manager and worker Services share this dedicated namespace.
        return {**result, "ready": True, "origin": "http://" + handle.name + ":3084", "endpoint": proof}

    async def ready(self, handle, route_token):
        from k8s_worker_proofs import authenticated_ready
        state = await self.inspect_worker(handle)
        require(state["ready"], "WORKER_PREPARING")
        result = await authenticated_ready(handle, state, route_token, self.worker_http)
        after = await self.inspect_worker(handle)
        require(after.get("endpoint") == state["endpoint"] and after["ready"], "WORKER_CHANGED_DURING_READY")
        return result

    async def delete_worker(self, handle):
        spec = self.check_handle(handle)
        objects = self.bound_manifests(spec, handle)
        # Do not delete PVCs or bootstrap Secrets. Their retention is separate policy.
        deletions = []
        # Prove both objects before touching either one. A mismatch must leave routing intact.
        for plural, obj, resource_uid in (("services", objects["service"], handle.service_uid), ("pods", objects["pod"], handle.pod_uid)):
            path = self.base + "/" + plural + "/" + handle.name
            status, observed = await self.call("GET", path)
            if status == 404:
                continue
            require(status == 200, "RESOURCE_READ_REFUSED")
            # A terminating object can be observed repeatedly; still check exact UID/labels.
            require(self.verify(obj, observed, terminating=True) == resource_uid, "DELETE_UID_CHANGED")
            if not observed["metadata"].get("deletionTimestamp"):
                deletions.append((path, resource_uid))
        for path, resource_uid in deletions:
            status, _ = await self.call("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions",
                "preconditions": {"uid": resource_uid}, "propagationPolicy": "Foreground", "gracePeriodSeconds": 30})
            require(status in (200, 202, 404), "RESOURCE_DELETE_REFUSED")
        observation = await self.observe_deleted(handle)
        return {"api_absent": observation["api_absent"], "execution_gone": False,
                "reservation_releasable": False, "persistent_claims_deleted": False}

    async def observe_deleted(self, handle):
        spec = self.check_handle(handle)
        objects = self.bound_manifests(spec, handle)
        absent = []
        for plural, key, expected_uid in (("pods", "pod", handle.pod_uid), ("services", "service", handle.service_uid)):
            status, value = await self.call("GET", self.base + "/" + plural + "/" + handle.name)
            if status == 404:
                absent.append(True)
                continue
            require(status == 200, "RESOURCE_READ_REFUSED")
            require(self.verify(objects[key], value, terminating=True) == expected_uid, "DELETE_OBSERVATION_UID_CHANGED")
            absent.append(False)
        return {"api_absent": all(absent), "execution_gone": False, "reservation_releasable": False}

    async def list_owned(self):
        selector = urllib.parse.quote(PREFIX + "instance=" + self.config.instance, safe="")
        status, result = await self.call("GET", self.base + "/pods?limit=200&labelSelector=" + selector)
        require(status == 200 and isinstance(result, dict) and isinstance(result.get("items"), list), "LIST_REFUSED")
        # Never silently truncate reconciliation. Caller must handle a larger fleet explicitly.
        require(not result.get("metadata", {}).get("continue"), "LIST_PAGINATION_REQUIRED")
        rows = []
        for pod in result["items"]:
            require(isinstance(pod, dict), "LIST_OWNERSHIP_REFUSED")
            meta = pod.get("metadata", {})
            require(isinstance(meta, dict), "LIST_OWNERSHIP_REFUSED")
            labels = meta.get("labels", {})
            require(isinstance(labels, dict), "LIST_OWNERSHIP_REFUSED")
            try:
                owner = labels[PREFIX + "owner"]
                generation = int(labels[PREFIX + "generation"])
                spec = WorkerSpec(owner, generation, mode=labels[PREFIX + "mode"])
                require(labels[PREFIX + "generation"] == str(generation))
                require(meta.get("namespace") == self.config.namespace and labels.get(PREFIX + "instance") == self.config.instance and
                        meta.get("name") == spec.name and uid(meta.get("uid")))
            except (KeyError, TypeError, ValueError, ProviderError):
                raise ProviderError("LIST_OWNERSHIP_REFUSED") from None
            rows.append({"owner": owner, "generation": generation, "name": spec.name, "pod_uid": meta["uid"],
                         "mode": spec.mode,
                         "recovery_action": "MATCH_DURABLE_RESERVATION_BEFORE_ANY_MUTATION"})
        return rows
