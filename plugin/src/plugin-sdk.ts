/**
 * Local plugin SDK types — replaces the non-existent "openclaw/plugin-sdk/plugin-entry" package.
 */

export interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  execute: (id: string, params: any) => Promise<unknown>;
}

export interface PluginAPI {
  registerTool(def: ToolDefinition): void;
}

export interface PluginEntry {
  id: string;
  name: string;
  description: string;
  register(api: PluginAPI): void;
}

export function definePluginEntry(entry: PluginEntry): PluginEntry {
  return entry;
}
