import catalogData from "../../shared/model_catalog.json";

export interface ModelOption {
  value: string;
  label: string;
}

type Catalog = {
  models: Record<string, ModelOption>;
  groups: Record<string, string[]>;
  defaults: Record<string, string>;
};

const catalog = catalogData as Catalog;

export function modelOption(modelKey: string): ModelOption {
  const model = catalog.models[modelKey];
  if (!model) throw new Error(`Unknown model key: ${modelKey}`);
  return { value: model.value, label: model.label };
}

export function modelOptions(group: string): ModelOption[] {
  const keys = catalog.groups[group];
  if (!keys) throw new Error(`Unknown model group: ${group}`);
  return keys.map(modelOption);
}

export function defaultModel(role: string): string {
  const modelKey = catalog.defaults[role];
  if (!modelKey) throw new Error(`Unknown default model role: ${role}`);
  return modelOption(modelKey).value;
}

export const LLM_MODELS = modelOptions("llm");
export const AGENT_MODELS = modelOptions("agent");
export const TINKER_MODELS = modelOptions("tinker");

export const DEFAULT_LLM_MODEL = defaultModel("llm");
export const DEFAULT_AGENT_MODEL = defaultModel("agent");
export const DEFAULT_TINKER_MODEL = defaultModel("tinker");
