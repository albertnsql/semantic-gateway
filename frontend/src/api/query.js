/**
 * api/query.js — POST /query API call.
 */
import apiClient from './client';

/** Turns kept in the history payload. The gateway also slices to the last 5. */
const MAX_HISTORY_TURNS = 10;

/**
 * Extract the assistant-visible text from a gateway response.
 *
 * The narrative summary is the agent's actual answer; `message` covers the
 * schema_question / out_of_scope / rejected envelopes which carry prose there
 * instead. Without this, agent turns serialise to '' and the model sees the
 * user's past questions but none of its own answers.
 *
 * @param {object} response - A GatewayResponse
 * @returns {string}
 */
export function agentTextFromResponse(response) {
  if (!response) return '';
  return response.narrative_summary || response.message || '';
}

/**
 * Normalise a page's local turn list into the wire history format.
 *
 * Both the dashboard chat and the standalone query page funnel through this so
 * the two cannot drift apart again — they store turns in different local
 * shapes, so each passes an already role-tagged list and this owns the rest
 * (blank-dropping, ordering, truncation).
 *
 * Blank turns are dropped rather than sent as '': an empty assistant turn is
 * worse than no turn, because it tells the model it replied with nothing.
 *
 * @param {Array<{role: string, content: string}>} turns
 * @returns {Array<{role: string, content: string}>}
 */
export function buildHistory(turns = []) {
  return turns
    .filter(t => t && typeof t.content === 'string' && t.content.trim() !== '')
    .map(t => ({
      role: t.role === 'user' ? 'user' : 'agent',
      content: t.content.trim(),
    }))
    .slice(-MAX_HISTORY_TURNS);
}

/**
 * Submit a natural language query to the gateway.
 *
 * @param {string} queryText - Natural language question
 * @param {Array}  history   - Conversational history [{ role, content }]
 * @param {object} options   - Query options
 * @param {boolean} options.include_sql
 * @param {boolean} options.include_lineage
 * @param {boolean} options.dry_run
 * @param {number}  options.max_rows
 * @returns {Promise<GatewayResponse>}
 */
export async function postQuery(queryText, history = [], options = {}) {
  const payload = {
    query: queryText,
    history: buildHistory(history),
    dashboard_context: options.dashboard_context || null,
    options: {
      max_rows: options.max_rows ?? 1000,
      include_sql: options.include_sql ?? true,
      include_lineage: options.include_lineage ?? true,
      dry_run: options.dry_run ?? false,
    },
  };
  try {
    const { data } = await apiClient.post('/query', payload);
    return data;
  } catch (error) {
    if (error.response && error.response.status === 422) {
      return error.response.data;
    }
    throw error;
  }
}
