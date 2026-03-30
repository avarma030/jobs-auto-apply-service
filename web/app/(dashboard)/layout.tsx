"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { auth, profile as profileApi } from "@/lib/api";
import { clearToken, isAuthenticated, onAuthChanged } from "@/lib/auth";
import { Sidebar } from "@/components/layout/Sidebar";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [activeUserKey, setActiveUserKey] = useState<string | null>(null);

  const refreshActiveUser = useCallback(async () => {
    if (!isAuthenticated()) {
      setActiveUserKey(null);
      setCheckingAuth(false);
      router.replace("/login");
      return;
    }

    try {
      const user = await auth.me();
      setActiveUserKey(`${user.id}:${user.email}`);
      void profileApi.get().catch(() => {});
    } catch {
      clearToken();
      setActiveUserKey(null);
      router.replace("/login");
    } finally {
      setCheckingAuth(false);
    }
  }, [router]);

  useEffect(() => {
    setCheckingAuth(true);
    void refreshActiveUser();
    return onAuthChanged(() => {
      setCheckingAuth(true);
      void refreshActiveUser();
    });
  }, [refreshActiveUser]);

  if (checkingAuth) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50 text-sm text-gray-500">
        Loading your workspace...
      </div>
    );
  }

  if (!activeUserKey) {
    return null;
  }

  return (
    <div className="flex min-h-screen bg-gray-50">
      <Sidebar />
      <main key={activeUserKey} className="flex-1 flex flex-col overflow-hidden">{children}</main>
    </div>
  );
}
