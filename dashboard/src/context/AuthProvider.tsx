'use client';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useRouter } from 'next/navigation';
import { api, setUnauthorizedHandler } from '@/lib/api';
import { toRole } from '@/lib/guards';
import { permissionsFor, type Permissions } from '@/lib/permissions';
import { clearToken, getToken } from '@/lib/session';
import type { RegisterRequest, User } from '@/lib/types';

export type AuthStatus = 'loading' | 'authenticated' | 'unauthenticated';

export interface AuthContextValue {
  user: User | null;
  token: string | null;
  status: AuthStatus;
  can: Permissions;
  login: (username: string, password: string) => Promise<void>;
  register: (body: RegisterRequest) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [token, setTokenState] = useState<string | null>(null);
  const [status, setStatus] = useState<AuthStatus>('loading');
  const routerRef = useRef(router);
  routerRef.current = router;

  const dropSession = useCallback(() => {
    clearToken();
    setUser(null);
    setTokenState(null);
    setStatus('unauthenticated');
  }, []);

  // 401 anywhere => clear session and bounce to /login.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      dropSession();
      routerRef.current.replace('/login');
    });
    return () => setUnauthorizedHandler(null);
  }, [dropSession]);

  // Restore session on first load.
  useEffect(() => {
    const existing = getToken();
    if (!existing) {
      setStatus('unauthenticated');
      return;
    }
    let cancelled = false;
    api
      .me()
      .then((u) => {
        if (cancelled) return;
        setUser({ ...u, role: toRole(u.role) });
        setTokenState(existing);
        setStatus('authenticated');
      })
      .catch(() => {
        if (!cancelled) dropSession();
      });
    return () => {
      cancelled = true;
    };
  }, [dropSession]);

  const login = useCallback(async (username: string, password: string) => {
    await api.login({ username, password });
    const u = await api.me();
    setUser({ ...u, role: toRole(u.role) });
    setTokenState(getToken());
    setStatus('authenticated');
  }, []);

  const register = useCallback(
    async (body: RegisterRequest) => {
      await api.register(body);
      await login(body.username, body.password);
    },
    [login],
  );

  const logout = useCallback(() => {
    dropSession();
    routerRef.current.replace('/login');
  }, [dropSession]);

  const value = useMemo<AuthContextValue>(
    () => ({ user, token, status, can: permissionsFor(user?.role), login, register, logout }),
    [user, token, status, login, register, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
