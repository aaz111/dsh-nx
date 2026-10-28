/**
 * Client for the resident NXOpen worker (`python/nxworker.py`).
 *
 * NX is expensive to start (a full `run_journal.exe` launch costs ~25 s), so
 * the worker runs once and is driven through two directories of JSON files:
 * the client drops `req/<id>.json`, the worker answers `resp/<id>.json`.
 * Requests are serialised — one NX session cannot serve two journals at once.
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_WORKER = path.join(HERE, '..', 'python', 'nxworker.py');

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Kill a process and its children; NX spawns a small tree. */
async function killTree(pid) {
    if (pid === undefined || pid === null)
        return;
    await new Promise((resolve) => {
        const child = spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
        child.on('exit', () => resolve());
        child.on('error', () => resolve());
    });
}

/** Places NX gets installed: the Siemens folders first, then the usual app drives. */
const NX_INSTALL_ROOTS = [
    'C:\\Program Files\\Siemens',
    'C:\\Program Files\\Siemens PLM Software',
    'C:\\Program Files (x86)\\Siemens',
    'C:\\Siemens',
    'C:\\software',
    'C:\\apps',
    'D:\\Program Files\\Siemens',
    'D:\\Program Files\\Siemens PLM Software',
    'D:\\Siemens',
    'D:\\software',
    'D:\\apps',
];

/** Does `dir` look like an NX install root, i.e. does it hold NXBIN\run_journal.exe? */
function isNxHome(dir) {
    try {
        return fs.existsSync(path.join(dir, 'NXBIN', 'run_journal.exe'));
    }
    catch {
        // an unreadable drive is simply not an install
        return false;
    }
}

/** NX install root: the explicit option or UGII_BASE_DIR wins, else probe the standard places. */
function findNxHome() {
    if (process.env.UGII_BASE_DIR)
        return process.env.UGII_BASE_DIR;
    const candidates = [];
    for (const base of NX_INSTALL_ROOTS) {
        candidates.push(base);
        try {
            for (const entry of fs.readdirSync(base, { withFileTypes: true })) {
                if (entry.isDirectory())
                    candidates.push(path.join(base, entry.name));
            }
        }
        catch {
            // not installed there
        }
    }
    return candidates.find(isNxHome) ?? 'C:\\Program Files\\Siemens\\NX';
}

export class NxBridge {
    constructor(config = {}) {
        this.root = config.root ?? path.join(os.homedir(), '.dsh-nx', 'bridge');
        this.nxHome = config.nxHome ?? findNxHome();
        this.outputDir = config.outputDir ?? path.join(os.homedir(), '.dsh-nx', 'out');
        this.worker = config.worker ?? DEFAULT_WORKER;
        this.startTimeoutMs = config.startTimeoutMs ?? 240_000;
        this.requestTimeoutMs = config.requestTimeoutMs ?? 180_000;
        this.reqDir = path.join(this.root, 'req');
        this.respDir = path.join(this.root, 'resp');
        this.readyPath = path.join(this.root, 'ready.json');
        this.logPath = path.join(this.root, 'bridge.log');
        this.child = undefined;
        this.pid = undefined;
        this.queue = Promise.resolve();
        this.starting = undefined;
    }

    get runJournal() {
        return path.join(this.nxHome, 'NXBIN', 'run_journal.exe');
    }

    prepare() {
        for (const dir of [this.root, this.reqDir, this.respDir]) {
            fs.mkdirSync(dir, { recursive: true });
        }
    }

    /** Read the ready file of a live worker, or undefined. */
    readReady() {
        try {
            const ready = JSON.parse(fs.readFileSync(this.readyPath, 'utf8'));
            if (ready?.stopping === true)
                return undefined;
            if (ready?.pid !== undefined && !isAlive(ready.pid))
                return undefined;
            return ready;
        }
        catch {
            return undefined;
        }
    }

    /** Start the worker if it is not already answering. Idempotent. */
    async ensure() {
        const ready = this.readReady();
        if (ready !== undefined) {
            this.pid = ready.pid;
            return ready;
        }
        if (this.starting !== undefined)
            return this.starting;
        this.starting = this.start().finally(() => {
            this.starting = undefined;
        });
        return this.starting;
    }

    async start() {
        this.prepare();
        if (!fs.existsSync(this.runJournal)) {
            throw new Error(`NX journal runner not found: ${this.runJournal} — set the nxHome option or UGII_BASE_DIR`);
        }
        if (!fs.existsSync(this.worker)) {
            throw new Error(`NX worker script missing: ${this.worker}`);
        }
        // Stale files from a crashed run would be answered instead of ours.
        for (const stale of [this.readyPath, path.join(this.root, 'fatal.log')]) {
            try {
                fs.rmSync(stale, { force: true });
            }
            catch { /* ignore */ }
        }
        for (const name of safeReaddir(this.reqDir))
            fs.rmSync(path.join(this.reqDir, name), { force: true });
        for (const name of safeReaddir(this.respDir))
            fs.rmSync(path.join(this.respDir, name), { force: true });

        const child = spawn(this.runJournal, [this.worker], {
            cwd: path.dirname(this.worker),
            env: { ...process.env, NXBRIDGE_ROOT: this.root, NXBRIDGE_OUT: this.outputDir },
            windowsHide: true,
            stdio: 'ignore',
            // NX takes ~25 s to start and keeps a modelling session in memory:
            // detach it so it outlives this client and the next call reuses it.
            detached: true,
        });
        this.child = child;
        child.on('error', () => { /* surfaced through the ready timeout */ });
        child.unref();

        const deadline = Date.now() + this.startTimeoutMs;
        while (Date.now() < deadline) {
            const ready = this.readReady();
            if (ready !== undefined) {
                this.pid = ready.pid;
                return ready;
            }
            if (child.exitCode !== null && child.exitCode !== undefined)
                break;
            await sleep(ifDelay());
        }
        const fatal = this.readTail(path.join(this.root, 'fatal.log'));
        throw new Error(`NX worker did not become ready within ${Math.round(this.startTimeoutMs / 1000)} s`
            + (fatal === '' ? '' : `\n${fatal}`));
    }

    readTail(file, limit = 4000) {
        try {
            const text = fs.readFileSync(file, 'utf8');
            return text.length > limit ? text.slice(-limit) : text;
        }
        catch {
            return '';
        }
    }

    /** Send one command and resolve with its result (throws on worker error). */
    request(cmd, args = {}, options = {}) {
        const run = () => this.dispatch(cmd, args, options);
        const queued = this.queue.then(run, run);
        this.queue = queued.then(() => undefined, () => undefined);
        return queued;
    }

    async dispatch(cmd, args, options) {
        const timeoutMs = options.timeoutMs ?? this.requestTimeoutMs;
        await this.ensure();
        const id = options.id ?? randomUUID().replaceAll('-', '').slice(0, 8);
        const reqPath = path.join(this.reqDir, `${id}.json`);
        const respPath = path.join(this.respDir, `${id}.json`);
        fs.writeFileSync(reqPath, JSON.stringify({ id, cmd, args }), 'utf8');
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            if (fs.existsSync(respPath)) {
                const text = fs.readFileSync(respPath, 'utf8');
                fs.rmSync(respPath, { force: true });
                const resp = JSON.parse(text);
                if (resp.ok !== true) {
                    const error = new Error(resp.error ?? 'NX command failed');
                    error.command = cmd;
                    error.trace = resp.trace;
                    throw error;
                }
                return resp.result;
            }
            await sleep(ifDelay());
        }
        fs.rmSync(reqPath, { force: true });
        // A hung command blocks the whole session: drop NX so the next call restarts.
        await this.stop(`timeout after ${timeoutMs} ms on ${cmd}`);
        throw new Error(`NX command "${cmd}" timed out after ${Math.round(timeoutMs / 1000)} s; the NX session was stopped`);
    }

    /** Ask the worker to exit, then make sure the process is gone. */
    async stop(reason = 'requested') {
        const pid = this.pid ?? this.readReady()?.pid;
        if (pid !== undefined) {
            try {
                fs.writeFileSync(path.join(this.reqDir, `${randomUUID().slice(0, 8)}.json`),
                    JSON.stringify({ id: randomUUID().slice(0, 8), cmd: 'quit', args: { reason } }), 'utf8');
            }
            catch { /* ignore */ }
        }
        await sleep(300);
        await killTree(pid);
        this.child = undefined;
        this.pid = undefined;
        try {
            fs.rmSync(this.readyPath, { force: true });
        }
        catch { /* ignore */ }
    }

    /** Stop and start again (recovers from a crashed or wedged session). */
    async restart() {
        await this.stop('restart');
        return this.start();
    }

    status() {
        const ready = this.readReady();
        return {
            root: this.root,
            nxHome: this.nxHome,
            worker: this.worker,
            running: ready !== undefined,
            ...(ready === undefined ? {} : { pid: ready.pid, release: ready.release, commands: ready.commands }),
        };
    }
}

function ifDelay() {
    return 120;
}

function isAlive(pid) {
    if (pid === undefined || pid === null)
        return false;
    try {
        process.kill(pid, 0);
        return true;
    }
    catch {
        return false;
    }
}

function safeReaddir(dir) {
    try {
        return fs.readdirSync(dir);
    }
    catch {
        return [];
    }
}
