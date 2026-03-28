import * as React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Stats } from "@/lib/types";
import { Briefcase, CheckCircle, Clock, TrendingUp, Trophy, XCircle } from "lucide-react";

interface Props {
  stats: Stats;
}

const cards: Array<{
  key: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  color: string;
  bg: string;
  suffix?: string;
}> = [
  { key: "total_scraped", label: "Jobs Found", icon: Briefcase, color: "text-blue-600", bg: "bg-blue-50" },
  { key: "total_applied", label: "Applied", icon: CheckCircle, color: "text-green-600", bg: "bg-green-50" },
  { key: "total_interviewing", label: "Interviewing", icon: Clock, color: "text-yellow-600", bg: "bg-yellow-50" },
  { key: "total_offered", label: "Offers", icon: Trophy, color: "text-purple-600", bg: "bg-purple-50" },
  { key: "this_week_applied", label: "This Week", icon: TrendingUp, color: "text-indigo-600", bg: "bg-indigo-50" },
  { key: "success_rate", label: "Success Rate", icon: TrendingUp, color: "text-emerald-600", bg: "bg-emerald-50", suffix: "%" },
];

export function StatsCards({ stats }: Props) {
  return (
    <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
      {cards.map(({ key, label, icon: Icon, color, bg, suffix }) => (
        <Card key={key}>
          <CardContent className="pt-4">
            <div className="flex items-center gap-3">
              <div className={`p-2 rounded-lg ${bg}`}>
                <Icon className={`h-4 w-4 ${color}`} />
              </div>
              <div>
                <p className="text-xs text-muted-foreground">{label}</p>
                <p className="text-2xl font-bold">
                  {stats[key as keyof Stats] as number}
                  {suffix}
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
