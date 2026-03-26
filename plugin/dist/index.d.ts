/**
 * EACN3 — Native OpenClaw plugin entry point.
 *
 * Registers the same 38 tools as server.ts but via api.registerTool().
 * All logic delegates to the same src/ modules.
 */
declare const _default: {
    id: string;
    name: string;
    description: string;
    register(api: any): void;
};
export default _default;
