"use client";

import { useEffect, useState } from "react";
import { auth as authApi } from "@/lib/api";

interface TopBarProps {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
}

export function TopBar({ title, subtitle, action }: TopBarProps) {
  const [email, setEmail] = useState("");

  useEffect(() => {
    authApi.me().then((u) => setEmail(u.email)).catch(() => {});
  }, []);

  return (
    <header className="h-16 border-b bg-white flex items-center px-6 gap-4">
      <div className="flex-1">
        <h2 className="text-lg font-semibold text-gray-900">{title}</h2>
        {subtitle && <p className="text-xs text-gray-500">{subtitle}</p>}
      </div>
      {action}
      {email && (
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-full bg-blue-600 flex items-center justify-center text-white text-xs font-bold">
            {email[0].toUpperCase()}
          </div>
          <span className="text-sm text-gray-600 hidden sm:block">{email}</span>
        </div>
      )}
    </header>
  );
}
