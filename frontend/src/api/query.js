/**
 * api/query.js — POST /query API call.
 */
import apiClient from './client';

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
    history: history.map(h => ({
      role: h.role === 'user' ? 'user' : 'agent',
      content: h.content || ''
    })),
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
