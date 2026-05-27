const assert = require("node:assert/strict");

const { ONBOARDING_STEPS, pendingSteps } = require("../dist/shared/onboardingSteps.js");

function baseState(overrides = {}) {
  return {
    seenSteps: [],
    featureFlags: { moments: true, memory: true },
    googleConnected: false,
    enabledConnectors: [],
    hasLlmApiKey: false,
    onboardingComplete: false,
    servicesReady: false,
    ...overrides,
  };
}

const ids = ONBOARDING_STEPS.map((step) => step.id);
assert.equal(ids[ids.indexOf("models_keys") + 1], "agent_setup");
assert.equal(ids[ids.indexOf("agent_setup") + 1], "getting_ready");

const returningUpdate = baseState({
  seenSteps: ["welcome", "tabracadabra", "chat", "tadas", "memex"],
  googleConnected: true,
  enabledConnectors: ["screen", "accessibility"],
  hasLlmApiKey: true,
  onboardingComplete: true,
  servicesReady: true,
});
assert.deepEqual(pendingSteps(returningUpdate).map((step) => step.id), ["agent_setup"]);

const returningAfterSeen = baseState({
  ...returningUpdate,
  seenSteps: [...returningUpdate.seenSteps, "agent_setup"],
});
assert.deepEqual(pendingSteps(returningAfterSeen), []);

console.log("onboarding step tests passed");
