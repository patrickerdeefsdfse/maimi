import { createHash } from "node:crypto";
import { lstatSync, readFile, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { isAbsolute, join, relative, sep } from "node:path";
import { promisify } from "node:util";
import { createRequire } from "node:module";
import { resolve } from "node:path";
const { valid } = createRequire(resolve(process.argv[2], "../package.json"))("semver");
//#region ../../../r116/upstream/apps/desktop/src/core-package-set.ts
/** Private package installed beside dsh to boot the Desktop Host process. */
const DESKTOP_HOST_PACKAGE = "@deepseek-ai/dsh-desktop-host";
//#endregion
//#region ../../../r116/upstream/apps/desktop/src/release.ts
/** Immutable version identity shared by one Electron shell and its bundled dsh runtime. */
function isRecord(value) {
	return typeof value === "object" && value !== null;
}
/** Validate release data read from an installed or packaged filesystem resource. */
function parseDesktopRelease(value) {
	if (!isRecord(value) || value.schemaVersion !== 1 || typeof value.version !== "string" || valid(value.version) === null || value.hostProtocolVersion !== 4 || typeof value.nodeVersion !== "string" || valid(value.nodeVersion) === null || typeof value.pnpmVersion !== "string" || valid(value.pnpmVersion) === null) throw new Error("dsh desktop: invalid desktop release metadata");
	return {
		schemaVersion: 1,
		version: value.version,
		hostProtocolVersion: 4,
		nodeVersion: value.nodeVersion,
		pnpmVersion: value.pnpmVersion
	};
}
//#endregion
//#region ../../../r116/upstream/apps/desktop/src/runtime-tree.ts
/** Relocatable, integrity-recorded production packages carried by one Desktop release. */
/** Descriptor at the root of the immutable Desktop resource tree. */
const DESKTOP_RUNTIME_FILE = "desktop-runtime.json";
const PACKAGE_NAME = /^(?:@[a-z0-9][a-z0-9._~-]*\/[a-z0-9][a-z0-9._~-]*|[a-z0-9][a-z0-9._~-]*)$/u;
const readRuntimeFile = promisify(readFile);
function record(value) {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}
/**
* Resolve one portable resource path without permitting traversal or absolute paths.
* @param root - Runtime root.
* @param path - Slash-separated relative path from durable metadata.
* @returns Absolute resource path.
*/
function runtimePath(root, path) {
	if (path === "" || isAbsolute(path) || path.includes("\\") || path.includes(":") || path.split("/").some((part) => part === "" || part === "." || part === "..")) throw new Error(`desktop runtime: invalid relative path ${JSON.stringify(path)}`);
	return join(root, ...path.split("/"));
}
function runtimeFiles(root) {
	const files = [];
	const visit = (directory) => {
		for (const entry of readdirSync(directory, { withFileTypes: true })) {
			const path = join(directory, entry.name);
			const name = relative(root, path).split(sep).join("/");
			if (name === "desktop-runtime.json") continue;
			if (entry.isDirectory()) visit(path);
			else if (entry.isFile()) files.push({
				path,
				name
			});
			else throw new Error(`desktop runtime: unsupported filesystem entry ${name}`);
		}
	};
	visit(root);
	return files;
}
function runtimeFile(path, name, body) {
	return {
		path: name,
		bytes: body.byteLength,
		sha256: createHash("sha256").update(body).digest("hex"),
		executable: process.platform !== "win32" && (lstatSync(path).mode & 73) !== 0
	};
}
/**
* Inventory a materialized runtime without following links or including its descriptor.
* @param root - Self-contained runtime directory.
* @returns Sorted final-file inventory; executable permissions are false on Windows.
*/
function inventoryDesktopRuntime(root) {
	return runtimeFiles(root).map(({ path, name }) => runtimeFile(path, name, readFileSync(path))).sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
}
async function inventoryRuntimeConcurrently(root) {
	const entries = runtimeFiles(root);
	const files = [];
	const remaining = entries.values();
	let failed = false;
	const workers = Array.from({ length: Math.min(8, entries.length) }, async () => {
		while (!failed) {
			const next = remaining.next();
			if (next.done) return;
			const { path, name } = next.value;
			try {
				files.push(runtimeFile(path, name, await readRuntimeFile(path)));
			} catch (error) {
				failed = true;
				throw error;
			}
		}
	});
	const results = await Promise.allSettled(workers);
	for (const result of results) if (result.status === "rejected") throw result.reason;
	return files.sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
}
/**
* Seal the final runtime tree after materialization and native signing.
* @param root - Runtime output directory.
* @param release - Matching shell, dsh, Host, and executable versions.
* @param sharedNames - Release-owned packages supplied to plugins.
* @param target - Platform and architecture selected by runtime preparation.
* @returns Descriptor written beside the production packages.
*/
function writeDesktopRuntime(root, release, sharedNames, target = process) {
	return writeRuntimeDescriptor(root, {
		...runtimeMetadata(root, release, sharedNames, target),
		files: inventoryDesktopRuntime(root)
	});
}
/**
* Seal a materialized runtime with bounded concurrent reads, draining readers before failure returns.
* @param root - Runtime output directory.
* @param release - Matching shell, dsh, Host, and executable versions.
* @param sharedNames - Release-owned packages supplied to plugins.
* @param target - Platform and architecture selected by runtime preparation.
* @returns Descriptor written after every final file has been inventoried.
*/
async function writeDesktopRuntimeAsync(root, release, sharedNames, target = process) {
	return writeRuntimeDescriptor(root, {
		...runtimeMetadata(root, release, sharedNames, target),
		files: await inventoryRuntimeConcurrently(root)
	});
}
function runtimeMetadata(root, release, sharedNames, target) {
	const sharedPackages = [...new Set(sharedNames)].sort().map((name) => {
		if (!PACKAGE_NAME.test(name)) throw new Error(`desktop runtime: invalid shared package ${name}`);
		const path = `node_modules/${name}`;
		const manifest = JSON.parse(readFileSync(join(runtimePath(root, path), "package.json"), "utf8"));
		if (!record(manifest) || manifest.name !== name || typeof manifest.version !== "string" || valid(manifest.version) === null) throw new Error(`desktop runtime: invalid shared package manifest ${name}`);
		return {
			name,
			version: manifest.version,
			path
		};
	});
	return {
		schemaVersion: 1,
		release,
		platform: target.platform,
		arch: target.arch,
		sharedPackages
	};
}
function writeRuntimeDescriptor(root, descriptor) {
	writeFileSync(join(root, DESKTOP_RUNTIME_FILE), `${JSON.stringify(descriptor, void 0, 2)}\n`);
	return descriptor;
}
/**
* Read packaged metadata and check shared package records.
* @param root - Current application's runtime resources.
* @returns Runtime metadata whose release compatibility is verified during packaging.
*/
function readDesktopRuntime(root) {
	const value = JSON.parse(readFileSync(join(root, DESKTOP_RUNTIME_FILE), "utf8"));
	if (!record(value) || typeof value.platform !== "string" || typeof value.arch !== "string" || !Array.isArray(value.sharedPackages) || !Array.isArray(value.files)) throw new Error("desktop runtime: invalid descriptor");
	if (!record(value.release) || typeof value.release.version !== "string" || typeof value.release.nodeVersion !== "string" || typeof value.release.pnpmVersion !== "string") throw new Error("desktop runtime: invalid release fields");
	const release = value.release;
	const sharedPackages = value.sharedPackages.map((entry) => {
		if (!record(entry) || typeof entry.name !== "string" || !PACKAGE_NAME.test(entry.name) || typeof entry.version !== "string" || valid(entry.version) === null || entry.path !== `node_modules/${entry.name}`) throw new Error("desktop runtime: invalid shared package record");
		return {
			name: entry.name,
			version: entry.version,
			path: entry.path
		};
	});
	if (new Set(sharedPackages.map((entry) => entry.name)).size !== sharedPackages.length) throw new Error("desktop runtime: duplicate shared package");
	const files = value.files;
	for (const name of ["@deepseek-ai/dsh", DESKTOP_HOST_PACKAGE]) if (sharedPackages.find((entry) => entry.name === name)?.version !== release.version) throw new Error(`desktop runtime: missing or mismatched ${name}`);
	return {
		schemaVersion: value.schemaVersion,
		release,
		platform: value.platform,
		arch: value.arch,
		sharedPackages,
		files
	};
}
/**
* Verify every packaged runtime file against its recorded bytes and permissions at build time.
* @param root - Materialized runtime resources.
* @param expectedVersion - Version the bundled runtime must declare: the product version ordinarily,
* and a rewritten one for installed-update qualification.
* @param target - Required execution target; defaults to the current process.
* @returns Validated runtime descriptor.
*/
async function verifyDesktopRuntime(root, expectedVersion, target = process) {
	const descriptor = readDesktopRuntime(root);
	if (descriptor.schemaVersion !== 1 || descriptor.platform !== target.platform || descriptor.arch !== target.arch) throw new Error("desktop runtime: invalid descriptor or incompatible platform/architecture");
	const release = parseDesktopRelease(descriptor.release);
	if (release.version !== expectedVersion) throw new Error(`desktop runtime: bundled ${release.version} is not the expected ${expectedVersion}`);
	for (const entry of descriptor.sharedPackages) {
		const manifest = JSON.parse(readFileSync(join(runtimePath(root, entry.path), "package.json"), "utf8"));
		if (!record(manifest) || manifest.name !== entry.name || manifest.version !== entry.version) throw new Error(`desktop runtime: shared package metadata mismatch for ${entry.name}`);
	}
	const actual = await inventoryRuntimeConcurrently(root);
	const comparable = (items) => process.platform === "win32" ? items.map(({ executable: _executable, ...item }) => item) : items;
	if (JSON.stringify(comparable(descriptor.files)) !== JSON.stringify(comparable(actual))) throw new Error("desktop runtime: integrity verification failed");
	return descriptor;
}
//#endregion
export { DESKTOP_RUNTIME_FILE, inventoryDesktopRuntime, readDesktopRuntime, runtimePath, verifyDesktopRuntime, writeDesktopRuntime, writeDesktopRuntimeAsync };
