import { motion } from "framer-motion";
import { AppStat, useApi } from "../api";
import { Card, Empty, Skeleton, fmtTime } from "../components/bits";
import { Stagger, StaggerItem } from "../components/motion";
import { Icon } from "../components/icons";

export default function Analytics() {
  const { data, loading } = useApi<{ apps: AppStat[] }>("/analytics/apps");
  const apps = data?.apps ?? [];
  const max = Math.max(1, ...apps.map((a) => a.run_count));

  return (
    <div className="space-y-5">
      <p className="text-sm text-zinc-500">
        What you actually use the assistant for, most-used first.
      </p>
      {loading ? (
        <Card className="space-y-5 p-5">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="space-y-2">
              <Skeleton className="h-3.5 w-1/3" />
              <Skeleton className="h-2 w-full rounded-full" />
            </div>
          ))}
        </Card>
      ) : apps.length ? (
        <Card className="p-5">
          <Stagger className="space-y-5" gap={0.06}>
            {apps.map((a) => (
              <StaggerItem key={a.launch_package ?? a.app_name}>
                <div>
                  <div className="mb-1.5 flex items-center justify-between gap-3 text-sm">
                    <span className="flex items-center gap-2 text-zinc-200">
                      <span className="text-lg">{a.emoji}</span>
                      {a.app_name}
                    </span>
                    <span className="font-mono text-[11px] text-zinc-500">
                      {a.run_count} runs · {Math.round(a.success_rate * 100)}% ok
                      · avg {a.avg_steps} steps · {fmtTime(a.last_used_at)}
                    </span>
                  </div>
                  <div className="h-2.5 overflow-hidden rounded-full bg-white/5">
                    <motion.div
                      className="h-full origin-left rounded-full bg-brand"
                      style={{ width: `${(a.run_count / max) * 100}%` }}
                      initial={{ scaleX: 0 }}
                      whileInView={{ scaleX: 1 }}
                      viewport={{ once: true }}
                      transition={{ duration: 0.9, ease: [0.22, 1, 0.36, 1] }}
                    />
                  </div>
                </div>
              </StaggerItem>
            ))}
          </Stagger>
        </Card>
      ) : (
        <Empty icon={<Icon.apps className="h-6 w-6" />}>No usage yet.</Empty>
      )}
    </div>
  );
}
