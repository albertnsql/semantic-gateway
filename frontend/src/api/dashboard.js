import { postQuery } from './query';
import apiClient from './client';

/**
 * Fetch a dashboard widget's data via the dedicated direct-Snowflake endpoint.
 * Bypasses the LLM pipeline entirely — zero LLM calls, ~10x faster than fetchDashboardQuery.
 *
 * @param {string} widgetId  - One of the registered widget IDs (e.g. 'mrr_kpi', 'mrr_trend')
 * @param {Object} filters   - Optional filter params
 * @param {string[]} filters.planTypes - e.g. ['Enterprise', 'Pro']
 * @param {string[]} filters.contentTypes
 * @param {string[]} filters.countries
 * @returns {Promise<{ status: 'success'|'error', data?: any[], error?: string, cacheHit?: boolean }>}
 */
export async function fetchDashboardWidget(widgetId, { planTypes = [], years = [], contentTypes = [], countries = [] } = {}) {
  try {
    const params = {};
    if (planTypes.length > 0) params.plan_types = planTypes.join(',');
    if (years.length > 0)     params.years       = years.join(',');
    if (contentTypes.length > 0) params.content_types = contentTypes.join(',');
    if (countries.length > 0)    params.countries     = countries.join(',');

    const response = await apiClient.get(`/dashboard/${widgetId}`, { params });
    const body = response.data;

    if (!body || !Array.isArray(body.data)) {
      return { status: 'error', error: 'Unexpected response shape from dashboard endpoint' };
    }

    return {
      status:   'success',
      data:     body.data,
      cacheHit: body.cache_hit ?? false,
    };
  } catch (error) {
    const message = error.response?.data?.detail?.message
      || error.response?.data?.message
      || error.message
      || 'Dashboard API connection failed';
    return { status: 'error', error: message };
  }
}

/**
 * Legacy: Fetch dashboard data via the full NL query pipeline.
 * Kept as a fallback — do NOT use this for dashboard widgets.
 *
 * @param {string} queryText - The natural language query
 * @param {number} maxRows   - Maximum number of rows to return
 */
export async function fetchDashboardQuery(queryText, maxRows = 50) {
  try {
    const result = await postQuery(queryText, [], {
      max_rows: maxRows,
      include_sql: false,
      include_lineage: false,
    });

    if (result.status !== 'success' || !result.result || !result.result.data) {
      return { status: 'error', error: result.error || 'Query failed or rejected' };
    }

    return { status: 'success', data: result.result.data };
  } catch (error) {
    return { status: 'error', error: error.message || 'API connection failed' };
  }
}
