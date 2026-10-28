/**
 * dsh-nx — Siemens NX tools for dsh.
 *
 * A resident NXOpen session (`python/nxworker.py`, started through
 * run_journal.exe) is driven over JSON files by the tools in ./tools.js.
 * Nothing here needs NX's GUI: the session runs headless and survives between
 * turns, so the next call reuses it instead of paying the ~25 s start again.
 *
 * The optional sidebar panel (`client.js`) reads the same bridge through two
 * HTTP routes registered here, so looking at NX state never costs a tool call.
 */
import os from 'node:os';
import path from 'node:path';
import { NxBridge } from './bridge.js';
import { createNxTools } from './tools.js';
import { GUIDE } from './prompt.js';
import { registerPanelRoutes } from './panel.js';

/** Artefact folder when the host configures none: a private place under the home directory. */
function resolveOutputDir(config) {
    return config.outputDir ?? process.env.DSH_NX_OUT ?? path.join(os.homedir(), '.dsh-nx', 'out');
}

export const name = 'dsh-nx';
export const inject = ['tools'];

export function apply(ctx, config = {}) {
    const outputDir = resolveOutputDir(config);
    const bridge = new NxBridge({
        root: config.root,
        nxHome: config.nxHome,
        worker: config.worker,
        outputDir,
        startTimeoutMs: config.startTimeoutMs ?? 300_000,
        requestTimeoutMs: config.requestTimeoutMs ?? 240_000,
    });

    const disposers = createNxTools(bridge).map((tool) => ctx.tools.register(tool));
    ctx.effect(() => () => {
        for (const dispose of disposers) {
            try {
                dispose();
            }
            catch {
                // the registry is already gone
            }
        }
    }, 'dsh-nx.tools');

    // The panel routes are optional: a host without a browser carrier simply
    // registers no routes and the tools keep working.
    if (config.panel !== false) {
        ctx.inject(['webServer'], (panelCtx) => {
            const dispose = registerPanelRoutes({
                webServer: panelCtx.webServer,
                bridge,
                outputDir,
            });
            panelCtx.effect(() => () => {
                try {
                    dispose();
                }
                catch {
                    // the server is already gone
                }
            }, 'dsh-nx.panel');
        });
    }

    // The NX session itself is intentionally left running: it holds the open
    // parts between turns, and stopping it here would throw that away.
    ctx.inject(['systemPrompt'], (promptCtx) => {
        promptCtx.systemPrompt.section({
            name: 'dsh-nx',
            order: promptCtx.systemPrompt.getSectionOrder('MCP_SERVERS'),
            interpolate: false,
            text: GUIDE,
        });
    });
}
