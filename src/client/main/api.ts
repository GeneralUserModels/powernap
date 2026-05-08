/** REST client — thin fetch wrapper for the Tada server. */

export { setServerUrl, getServerUrl } from "../shared/api-core";
import { request } from "../shared/api-core";

// ── Moments ─────────────────────────────────────────────────
export const getMomentsTasks = () => request("GET", "/api/moments/tasks");
export const getMomentsResults = () => request("GET", "/api/moments/results");
