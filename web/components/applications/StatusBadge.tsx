import { Badge } from "@/components/ui/badge";

const STATUS_MAP: Record<string, { label: string; variant: "default" | "secondary" | "destructive" | "outline" | "success" | "warning" | "info" }> = {
  pending: { label: "Pending", variant: "secondary" },
  applied: { label: "Applied", variant: "success" },
  skipped: { label: "Skipped", variant: "outline" },
  failed: { label: "Failed", variant: "destructive" },
  interviewing: { label: "Interviewing", variant: "warning" },
  offered: { label: "Offered", variant: "info" },
  rejected: { label: "Rejected", variant: "destructive" },
  approved: { label: "Approved", variant: "info" },
};

export function StatusBadge({ status }: { status: string }) {
  const { label, variant } = STATUS_MAP[status] ?? { label: status, variant: "outline" as const };
  return <Badge variant={variant}>{label}</Badge>;
}
