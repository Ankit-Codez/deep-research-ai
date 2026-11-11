import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';
import { clearAuthSession } from '../api';

beforeEach(() => {
  clearAuthSession();
  history.replaceState(null, '', '/');
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
  // Each test must explicitly supply its backend; never contact live providers.
  vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('Unexpected network request in a test.'))));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });
