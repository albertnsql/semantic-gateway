/**
 * api/client.js — Axios instance for AI Semantic Gateway API.
 */
import axios from 'axios';

const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1',
  timeout: 90000,  // 90s — LLM + Snowflake pipeline can take 15-20s per query
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
