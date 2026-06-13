/**
 * api/metrics.js — Metric and health API calls.
 */
import apiClient from './client';

/** GET /health — ping the backend. */
export async function getHealth() {
  const { data } = await apiClient.get('/health');
  return data;
}

let cachedMetrics = null;

/** GET /metrics — returns array of MetricDefinition objects. */
export async function getMetrics() {
  if (cachedMetrics) return cachedMetrics;
  const { data } = await apiClient.get('/metrics');
  cachedMetrics = data;
  return data;
}

/** GET /metrics/:name — returns a single MetricDefinition. */
export async function getMetric(name) {
  const { data } = await apiClient.get(`/metrics/${name}`);
  return data;
}

/**
 * GET /lineage/:metric_name — returns lineage info.
 * Response shape: { metric_name, source_model, upstream_models, source_tables,
 *                   transformation_steps: [{model_name, layer, description}],
 *                   lineage_path: string[] }
 */
export async function getLineage(name) {
  const { data } = await apiClient.get(`/lineage/${name}`);
  return data;
}
