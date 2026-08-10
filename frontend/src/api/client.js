/**
 * api/client.js — Axios instance for AI Semantic Gateway API.
 */
import axios from 'axios';

const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1',
  // 90s — a normal query is ~3-5s (two LLM calls dominate; DuckDB itself is ms),
  // but a cold gateway can spend ~20-50s waking up and building the MetricFlow engine.
  timeout: 90000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Request interceptor — only log in dev mode to prevent data leaking to DevTools in prod
apiClient.interceptors.request.use((config) => {
  if (import.meta.env.DEV) {
    console.log(`[API] → ${config.method?.toUpperCase()} ${config.url}`);
  }
  return config;
});

// Response interceptor — only log in dev mode
apiClient.interceptors.response.use(
  (response) => {
    if (import.meta.env.DEV) {
      console.log(`[API] ← ${response.status} ${response.config.url}`);
    }
    return response;
  },
  (error) => {
    if (import.meta.env.DEV) {
      console.error(`[API] ✗ ${error.response?.status || 'NETWORK'} ${error.config?.url}`, error.response?.data || error.message);
    }
    return Promise.reject(error);
  }
);

export default apiClient;
