"use client";

import { useEffect, useRef, useState } from "react";
import { profile as profileApi } from "@/lib/api";
import type { Profile } from "@/lib/types";
import { TopBar } from "@/components/layout/TopBar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Save, Upload, Plus, Trash2 } from "lucide-react";

export default function ProfilePage() {
  const [profile, setProfile] = useState<Profile>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    profileApi.get().then((r) => setProfile(r.profile)).catch(() => {});
  }, []);

  function set<K extends keyof Profile>(key: K, value: Profile[K]) {
    setProfile((p) => ({ ...p, [key]: value }));
  }

  async function save() {
    setSaving(true);
    try {
      await profileApi.update(profile);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  }

  async function uploadResume(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      await profileApi.uploadResume(file);
      set("resume_path" as any, `data/uploads/${file.name}`);
    } finally {
      setUploading(false);
    }
  }

  const skills = profile.skills ?? [];
  const experience = profile.work_experience ?? [];
  const education = profile.education ?? [];

  return (
    <div className="flex-1 overflow-y-auto">
      <TopBar
        title="Profile"
        subtitle="Your information used for job applications"
        action={
          <Button onClick={save} disabled={saving} size="sm">
            <Save className="h-4 w-4 mr-2" />
            {saved ? "Saved!" : saving ? "Saving…" : "Save Profile"}
          </Button>
        }
      />
      <div className="p-6 space-y-6 max-w-4xl">

        {/* Personal Info */}
        <Card>
          <CardHeader><CardTitle className="text-base">Personal Info</CardTitle></CardHeader>
          <CardContent className="grid grid-cols-2 gap-4">
            <Field label="First Name" value={profile.first_name ?? ""} onChange={(v) => set("first_name", v)} />
            <Field label="Last Name" value={profile.last_name ?? ""} onChange={(v) => set("last_name", v)} />
            <Field label="Email" value={profile.email ?? ""} onChange={(v) => set("email", v)} type="email" />
            <Field label="Phone" value={profile.phone ?? ""} onChange={(v) => set("phone", v)} />
            <Field label="City" value={profile.address?.city ?? ""} onChange={(v) => set("address", { ...profile.address, city: v })} />
            <Field label="State / Country" value={profile.address?.state ?? ""} onChange={(v) => set("address", { ...profile.address, state: v })} />
            <Field label="LinkedIn URL" value={profile.social_links?.linkedin ?? ""} onChange={(v) => set("social_links", { ...profile.social_links, linkedin: v })} className="col-span-2" />
          </CardContent>
        </Card>

        {/* Professional */}
        <Card>
          <CardHeader><CardTitle className="text-base">Professional Summary</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <Field label="Headline" value={profile.headline ?? ""} onChange={(v) => set("headline", v)} placeholder="e.g. Senior Software Engineer" />
            <div>
              <Label className="text-xs text-muted-foreground">Summary</Label>
              <Textarea
                value={profile.summary ?? ""}
                onChange={(e) => set("summary", e.target.value)}
                placeholder="Brief professional bio…"
                className="mt-1"
                rows={3}
              />
            </div>
            <Field label="Years of Experience" value={String(profile.years_of_experience ?? "")} onChange={(v) => set("years_of_experience", Number(v))} type="number" />
          </CardContent>
        </Card>

        {/* Skills */}
        <Card>
          <CardHeader><CardTitle className="text-base">Skills</CardTitle></CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2 mb-3">
              {skills.map((s, i) => (
                <span key={i} className="flex items-center gap-1 bg-blue-50 text-blue-700 rounded-full px-3 py-1 text-sm">
                  {s}
                  <button onClick={() => set("skills", skills.filter((_, j) => j !== i))} className="text-blue-400 hover:text-blue-700">×</button>
                </span>
              ))}
            </div>
            <Input
              placeholder="Type a skill and press Enter"
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  const v = (e.target as HTMLInputElement).value.trim();
                  if (v && !skills.includes(v)) set("skills", [...skills, v]);
                  (e.target as HTMLInputElement).value = "";
                }
              }}
            />
          </CardContent>
        </Card>

        {/* Work Experience */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-base">Work Experience</CardTitle>
            <Button size="sm" variant="outline" onClick={() => set("work_experience", [...experience, { company: "", title: "", start_date: "" }])}>
              <Plus className="h-4 w-4 mr-1" /> Add
            </Button>
          </CardHeader>
          <CardContent className="space-y-6">
            {experience.map((exp, i) => (
              <div key={i} className="border rounded-lg p-4 space-y-3 relative">
                <button
                  className="absolute top-3 right-3 text-gray-400 hover:text-red-500"
                  onClick={() => set("work_experience", experience.filter((_, j) => j !== i))}
                >
                  <Trash2 className="h-4 w-4" />
                </button>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Company" value={exp.company} onChange={(v) => { const e2 = [...experience]; e2[i] = { ...exp, company: v }; set("work_experience", e2); }} />
                  <Field label="Title" value={exp.title} onChange={(v) => { const e2 = [...experience]; e2[i] = { ...exp, title: v }; set("work_experience", e2); }} />
                  <Field label="Start Date" value={exp.start_date} onChange={(v) => { const e2 = [...experience]; e2[i] = { ...exp, start_date: v }; set("work_experience", e2); }} placeholder="YYYY-MM" />
                  <Field label="End Date" value={exp.end_date ?? ""} onChange={(v) => { const e2 = [...experience]; e2[i] = { ...exp, end_date: v || undefined }; set("work_experience", e2); }} placeholder="YYYY-MM or leave blank" />
                </div>
              </div>
            ))}
            {experience.length === 0 && <p className="text-sm text-muted-foreground text-center py-4">No experience added yet.</p>}
          </CardContent>
        </Card>

        {/* Resume Upload */}
        <Card>
          <CardHeader><CardTitle className="text-base">Resume</CardTitle></CardHeader>
          <CardContent>
            <input ref={fileRef} type="file" accept=".pdf" className="hidden" onChange={uploadResume} />
            <div className="flex items-center gap-3">
              <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={uploading}>
                <Upload className="h-4 w-4 mr-2" />
                {uploading ? "Uploading…" : "Upload PDF"}
              </Button>
              {profile.resume_path && (
                <span className="text-sm text-green-700 bg-green-50 rounded px-2 py-1">
                  ✓ Resume uploaded
                </span>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function Field({
  label, value, onChange, type = "text", placeholder, className,
}: {
  label: string; value: string; onChange: (v: string) => void;
  type?: string; placeholder?: string; className?: string;
}) {
  return (
    <div className={className}>
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Input type={type} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} className="mt-1" />
    </div>
  );
}
