<template>
  <div class="guide-page">
    <header class="guide-hero">
      <div>
        <p class="eyebrow">Agent Observatory</p>
        <h1>使用方法</h1>
        <p class="lede">
          Agent Observatory 是一个本地优先、只读的多 agent 会话观测台。它把本机不同
          coding agent 的历史会话整理到统一界面中，方便浏览、搜索、分析和审计。
        </p>
      </div>
      <div class="hero-note">
        <BookOpen :size="18" aria-hidden="true" />
        <span>本页内容可直接作为日常使用和本地部署速查表。</span>
      </div>
    </header>

    <div class="guide-layout">
      <main class="guide-content">
        <section id="quick-start" class="guide-section">
          <div class="section-heading">
            <span class="section-number">01</span>
            <div>
              <h2>快速开始</h2>
              <p>启动服务后，在浏览器打开默认入口并登录。</p>
            </div>
          </div>
          <div class="info-grid">
            <article class="info-card">
              <span class="card-kicker">入口</span>
              <strong>http://127.0.0.1:8125</strong>
              <p>默认由 Supervisor 托管 API 和 Indexer Worker。</p>
            </article>
            <article class="info-card">
              <span class="card-kicker">定位</span>
              <strong>只读观测台</strong>
              <p>不会修改、重命名或删除任何 agent 的原始会话文件。</p>
            </article>
            <article class="info-card">
              <span class="card-kicker">数据</span>
              <strong>持续索引</strong>
              <p>Indexer 会发现本地数据源，并以增量方式更新历史记录。</p>
            </article>
          </div>
        </section>

        <section id="navigation" class="guide-section">
          <div class="section-heading">
            <span class="section-number">02</span>
            <div>
              <h2>主导航</h2>
              <p>从侧边栏进入不同的观测视图。</p>
            </div>
          </div>
          <div class="feature-list">
            <article v-for="item in navigationItems" :key="item.title" class="feature-item">
              <div class="feature-icon" aria-hidden="true">{{ item.icon }}</div>
              <div>
                <h3>{{ item.title }}</h3>
                <p>{{ item.description }}</p>
              </div>
            </article>
          </div>
        </section>

        <section id="history" class="guide-section">
          <div class="section-heading">
            <span class="section-number">03</span>
            <div>
              <h2>History：浏览会话</h2>
              <p>History 是查看原始会话内容最常用的入口。</p>
            </div>
          </div>
          <div class="steps">
            <div v-for="(step, index) in historySteps" :key="step.title" class="step">
              <span class="step-index">{{ index + 1 }}</span>
              <div>
                <h3>{{ step.title }}</h3>
                <p>{{ step.description }}</p>
              </div>
            </div>
          </div>
          <div class="callout callout--accent">
            <strong>Subagent 默认隐藏</strong>
            <p>
              History 默认只显示主会话。需要检查派遣任务时，再勾选
              <code>Show subagents</code>；child session 会显示 <code>subagent</code>
              标记，并保留 parent session 关系。
            </p>
          </div>
          <div class="callout">
            <strong>时间和 workspace</strong>
            <p>
              列表时间优先使用会话最后一条真实活动事件；如果源数据没有可靠时间，会显示
              <code>—</code>，不会把扫描时间误认为活动时间。类似
              <code>/tmp/tmp...</code> 的目录通常是临时执行 workspace（
              <code>ephemeral_task</code>），但仅凭路径不能证明它是 subagent。
            </p>
          </div>
        </section>

        <section id="projects" class="guide-section">
          <div class="section-heading">
            <span class="section-number">04</span>
            <div>
              <h2>Projects：按 workspace 聚合</h2>
              <p>用项目视角比较不同工作目录的会话和资源消耗。</p>
            </div>
          </div>
          <ul class="plain-list">
            <li>项目标题显示目录 basename，完整路径可悬停查看或复制。</li>
            <li>查看 session、turn、token 总量和最近活动时间。</li>
            <li>进入项目详情后，可以继续打开该 workspace 下的具体会话。</li>
            <li>workspace 会标记为 project、ephemeral、test、system 或 deleted 等类型。</li>
          </ul>
        </section>

        <section id="insights" class="guide-section">
          <div class="section-heading">
            <span class="section-number">05</span>
            <div>
              <h2>Insights：分析指标</h2>
              <p>从聚合指标中了解 agent 的使用量、延迟和质量信号。</p>
            </div>
          </div>
          <div class="metric-grid">
            <div v-for="metric in insightItems" :key="metric.title" class="metric-card">
              <strong>{{ metric.title }}</strong>
              <span>{{ metric.description }}</span>
            </div>
          </div>
        </section>

        <section id="memories" class="guide-section">
          <div class="section-heading">
            <span class="section-number">06</span>
            <div>
              <h2>Memories：复制 CLI 记忆</h2>
              <p>把当前机器上的有效记忆整理成可迁移的 Markdown 或 ZIP。</p>
            </div>
          </div>
          <div class="detail-grid">
            <div>
              <h3>来源</h3>
              <ul class="plain-list">
                <li>Codex / StepCode：读取 <code>memories_1.sqlite</code>。</li>
                <li>Claude：读取项目下的 <code>memory</code> 目录。</li>
                <li>DSH 和 OpenCode：没有明确记忆格式时显示为未检测到。</li>
              </ul>
            </div>
            <div>
              <h3>迁移</h3>
              <ul class="plain-list">
                <li>单条记忆可复制原文或带来源的 Markdown。</li>
                <li>多选后导出 Markdown 或 ZIP，并附带 <code>manifest.json</code>。</li>
                <li>敏感内容默认脱敏；完整复制需要 operator 或 admin。</li>
              </ul>
            </div>
          </div>
        </section>

        <section id="storage" class="guide-section">
          <div class="section-heading">
            <span class="section-number">07</span>
            <div>
              <h2>Storage：清理旧数据</h2>
              <p>按保留窗口删除各 agent 的历史数据，释放磁盘空间。</p>
            </div>
          </div>
          <div class="feature-list">
            <article class="feature-item">
              <div class="feature-icon" aria-hidden="true">⛁</div>
              <div>
                <h3>保留窗口</h3>
                <p>先选择保留最近 1 / 3 / 7 / 14 / 30 / 90 天，只有早于窗口且超过最低保留期（默认 24 小时）的文件才会进入计划。</p>
              </div>
            </article>
            <article class="feature-item">
              <div class="feature-icon" aria-hidden="true">◱</div>
              <div>
                <h3>Preview（只读）</h3>
                <p>预览会列出将删除的文件数、字节数以及会一并清理的索引行；预览本身不修改任何文件。</p>
              </div>
            </article>
            <article class="feature-item">
              <div class="feature-icon" aria-hidden="true">⌫</div>
              <div>
                <h3>执行与审计</h3>
                <p>只有 admin 能提交删除。请求先写入 system_commands，由本机 Supervisor 执行，并把释放空间写入 retention_runs 与 audit_logs。</p>
              </div>
            </article>
            <article class="feature-item">
              <div class="feature-icon" aria-hidden="true">⛨</div>
              <div>
                <h3>保护与熔断</h3>
                <p>AGENT_OBS_RETENTION_PROTECTED_ROOTS 可锁定目录，AGENT_OBS_RETENTION_ENABLED=false 可整体停用；计划与执行不一致时执行会被拒绝。</p>
              </div>
            </article>
          </div>
          <div class="steps">
            <article class="step">
              <h3>推荐流程</h3>
              <p>打开 Storage → 勾选要清理的数据类别 → 选择保留天数 → Preview cleanup → 确认无误后点击 Delete … now。执行完成后在 Recent runs 查看释放空间；页面会在清理进行中和完成后自动重新扫描，无需手动刷新（页头显示数据时间与是否来自服务端缓存）。</p>
            </article>
          </div>
        </section>

        <section id="details" class="guide-section">
          <div class="section-heading">
            <span class="section-number">08</span>
            <div>
              <h2>会话详情与导出</h2>
              <p>打开单个会话后，可以按不同视图审阅完整证据。</p>
            </div>
          </div>
          <div class="detail-grid">
            <div>
              <h3>可查看内容</h3>
              <ul class="plain-list">
                <li>用户消息、assistant 回复和 reasoning 摘要。</li>
                <li>工具调用、参数、输出、状态和失败信息。</li>
                <li>原始事件时间线，包括未知事件及其 <code>raw_json</code>。</li>
                <li>会话内全文搜索，并支持复制完整长文本。</li>
              </ul>
            </div>
            <div>
              <h3>导出格式</h3>
              <ul class="plain-list">
                <li><code>Text</code>：适合终端或纯文本归档。</li>
                <li><code>Markdown</code>：适合文档和评审。</li>
                <li><code>JSON</code>：保留结构化会话数据。</li>
              </ul>
            </div>
          </div>
        </section>

        <section id="deployment" class="guide-section">
          <div class="section-heading">
            <span class="section-number">09</span>
            <div>
              <h2>本地部署与运维</h2>
              <p>以下命令在仓库的 <code>backend</code> 目录执行。</p>
            </div>
          </div>
          <div class="command-list">
            <article v-for="command in commands" :key="command.title" class="command-card">
              <div class="command-card__header">
                <div>
                  <h3>{{ command.title }}</h3>
                  <p>{{ command.description }}</p>
                </div>
                <button
                  type="button"
                  class="copy-button"
                  :aria-label="`复制${command.title}`"
                  @click="copyCommand(command.title, command.command)"
                >
                  <Check v-if="copiedCommand === command.title" :size="14" aria-hidden="true" />
                  <Copy v-else :size="14" aria-hidden="true" />
                  {{ copiedCommand === command.title ? "已复制" : "复制" }}
                </button>
              </div>
              <pre><code>{{ command.command }}</code></pre>
            </article>
          </div>
          <div class="endpoint-card">
            <h3>健康检查接口</h3>
            <div v-for="endpoint in endpoints" :key="endpoint.path" class="endpoint-row">
              <code>{{ endpoint.method }}</code>
              <span>{{ endpoint.path }}</span>
              <small>{{ endpoint.description }}</small>
            </div>
          </div>
        </section>

        <section id="security" class="guide-section">
          <div class="section-heading">
            <span class="section-number">10</span>
            <div>
              <h2>权限与数据安全</h2>
              <p>观测数据和控制操作遵循最小权限原则。</p>
            </div>
          </div>
          <div class="security-list">
            <div v-for="item in securityItems" :key="item.title" class="security-item">
              <strong>{{ item.title }}</strong>
              <p>{{ item.description }}</p>
            </div>
          </div>
        </section>

        <section id="faq" class="guide-section guide-section--last">
          <div class="section-heading">
            <span class="section-number">11</span>
            <div>
              <h2>常见问题</h2>
              <p>遇到显示或索引问题时，可以先检查以下项目。</p>
            </div>
          </div>
          <div class="faq-list">
            <details v-for="item in faqItems" :key="item.question">
              <summary>{{ item.question }}</summary>
              <p>{{ item.answer }}</p>
            </details>
          </div>
        </section>
      </main>

      <aside class="guide-index" aria-label="使用方法目录">
        <span class="guide-index__label">本页目录</span>
        <a v-for="item in sectionLinks" :key="item.id" :href="`#${item.id}`">{{ item.label }}</a>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { BookOpen, Check, Copy } from "@lucide/vue";
import { ref } from "vue";

const copiedCommand = ref<string | null>(null);

const navigationItems = [
  { icon: "⌂", title: "Dashboard", description: "查看整体健康状态、最近会话和关键统计。" },
  { icon: "◷", title: "History", description: "按 workspace 浏览会话，搜索、查看详情和导出。" },
  { icon: "✦", title: "Memories", description: "浏览各 CLI 的记忆条目，复制原文或导出到另一台机器。" },
  { icon: "▣", title: "Projects", description: "按工作目录聚合 session、turn、token 和活动时间。" },
  { icon: "⌁", title: "Insights", description: "查看 Token、延迟、工具失败率和质量信号。" },
  { icon: "⛁", title: "Storage", description: "预览并清理超过保留窗口的本地会话数据，操作写入审计日志。" },
  { icon: "✓", title: "Audit", description: "审阅用户登录、系统控制和权限相关审计事件。" },
  { icon: "⚙", title: "System", description: "管理员检查 API、PostgreSQL、Indexer 和 Supervisor。" },
];

const historySteps = [
  { title: "筛选", description: "按 agent、thread、workspace 或标题搜索；点击 Refresh 重新读取索引结果。" },
  { title: "分组", description: "会话按 cwd/workspace 分组，目录标题显示 basename，完整路径可悬停或复制。" },
  { title: "打开", description: "点击会话查看消息、reasoning、工具调用、事件时间线和 token 信息。" },
  { title: "导出", description: "在会话详情页选择 Text、Markdown 或 JSON，导出前端可复制的完整内容。" },
];

const insightItems = [
  { title: "Token usage", description: "输入、缓存输入、输出和 reasoning token。" },
  { title: "Turn latency", description: "Turn 的耗时分布和 P50/P95 等统计。" },
  { title: "Tool health", description: "工具调用量、延迟、失败率和状态。" },
  { title: "Quality signals", description: "会话结果、错误和可观测质量证据。" },
  { title: "Parent / child", description: "主会话和 subagent 的关系与调用链。" },
  { title: "Source health", description: "各 agent 数据源的发现、索引和健康状态。" },
];

const commands = [
  {
    title: "初始化",
    description: "检查依赖、配置数据库并创建管理员。",
    command: "cd backend\nuv sync\nAGENT_OBS_ADMIN_PASSWORD='change-this-password' \\\n  uv run agent-observatory init",
  },
  {
    title: "后台启动",
    description: "由 Supervisor 后台启动 API 和 Indexer Worker。",
    command: "uv run agent-observatory up",
  },
  {
    title: "状态与诊断",
    description: "查看组件状态，或检查本地依赖和配置。",
    command: "uv run agent-observatory status\nuv run agent-observatory doctor",
  },
  {
    title: "索引与日志",
    description: "手动扫描一次会话，或跟踪 worker 日志。",
    command: "uv run agent-observatory indexer --once\nuv run agent-observatory logs worker --follow",
  },
  {
    title: "停止或开发模式",
    description: "停止后台服务，或启动 API、Worker 和 Vite HMR。",
    command: "uv run agent-observatory down\nuv run agent-observatory dev",
  },
];

const endpoints = [
  { method: "GET", path: "/api/health/live", description: "进程存活检查" },
  { method: "GET", path: "/api/health/ready", description: "PostgreSQL 和 Indexer 就绪状态" },
  { method: "GET", path: "/api/system/status", description: "Supervisor 组件状态" },
  { method: "GET", path: "/api/system/logs", description: "系统日志查询" },
  { method: "GET", path: "/api/memories", description: "记忆条目筛选和分页" },
  { method: "POST", path: "/api/memories/export", description: "导出选中的记忆迁移包" },
  { method: "GET", path: "/api/retention/overview", description: "各 agent 数据目录占用" },
  { method: "POST", path: "/api/retention/preview", description: "生成只读清理计划" },
];

const securityItems = [
  { title: "角色权限", description: "viewer 负责查看，operator 可执行受控运维操作，admin 负责用户、权限和系统管理。" },
  { title: "事实来源", description: "PostgreSQL 保存会话、审计、命令和索引断点，是唯一持久化事实来源。" },
  { title: "原始文件", description: "索引过程只读 ~/.codex、~/.claude、~/.stepcode、~/.dsh 等源目录；只有 admin 在 Storage 明确提交清理计划时才会删除旧文件。" },
  { title: "敏感信息", description: "展示和导出会进行 secret redaction；不要提交 token、密码、dump、日志或截图。" },
];

const faqItems = [
  { question: "为什么会看到 /tmp/tmp... 这样的 workspace？", answer: "很多 agent executor 会为一次任务创建临时 sandbox。它会被标记为 ephemeral_task；这只表示目录生命周期短，不等价于 subagent。" },
  { question: "为什么默认看不到 subagent？", answer: "History 默认隐藏 child session，避免派遣任务把主会话列表刷满。勾选 Show subagents 后即可查看。" },
  { question: "为什么时间显示为 —？", answer: "源文件没有可解析的真实事件时间。系统不会用数据库扫描时间冒充活动时间，因此显示为未知。" },
  { question: "新会话没有立即出现怎么办？", answer: "先点击 History 的 Refresh；如果仍未出现，可以在 backend 目录执行 uv run agent-observatory indexer --once。" },
  { question: "页面更新后仍然显示旧内容怎么办？", answer: "浏览器可能缓存了旧 bundle，请执行 Ctrl/Cmd+Shift+R 强制刷新，并确认 /api/health 返回正常。" },
];

const sectionLinks = [
  { id: "quick-start", label: "快速开始" },
  { id: "navigation", label: "主导航" },
  { id: "history", label: "History" },
  { id: "projects", label: "Projects" },
  { id: "insights", label: "Insights" },
  { id: "memories", label: "Memories" },
  { id: "storage", label: "Storage" },
  { id: "details", label: "会话详情与导出" },
  { id: "deployment", label: "部署与运维" },
  { id: "security", label: "权限与安全" },
  { id: "faq", label: "常见问题" },
];

async function copyCommand(title: string, command: string) {
  try {
    await navigator.clipboard.writeText(command);
    copiedCommand.value = title;
    window.setTimeout(() => {
      if (copiedCommand.value === title) copiedCommand.value = null;
    }, 1600);
  } catch {
    copiedCommand.value = null;
  }
}
</script>

<style scoped>
.guide-page { max-width: 1180px; margin: 0 auto; padding: 32px 24px 56px; }
.guide-hero { display: flex; justify-content: space-between; gap: 28px; padding-bottom: 30px; border-bottom: 1px solid var(--border); }
.guide-hero h1 { margin: 4px 0 10px; font-size: 31px; letter-spacing: -0.03em; }
.lede { max-width: 720px; margin: 0; color: var(--fg-muted); font-size: 15px; line-height: 1.75; }
.hero-note { align-self: end; display: flex; max-width: 245px; gap: 9px; padding: 12px 14px; border: 1px solid rgba(110,159,248,.24); border-radius: var(--radius); background: var(--accent-weak); color: var(--fg-muted); font-size: 12px; line-height: 1.5; }
.guide-layout { display: grid; grid-template-columns: minmax(0, 1fr) 170px; gap: 44px; align-items: start; }
.guide-content { min-width: 0; }
.guide-section { padding: 34px 0; border-bottom: 1px solid var(--border); scroll-margin-top: 24px; }
.guide-section--last { border-bottom: 0; }
.section-heading { display: flex; gap: 13px; margin-bottom: 22px; }
.section-number { color: var(--accent-strong); font-family: var(--mono); font-size: 11px; padding-top: 5px; }
.section-heading h2 { margin: 0 0 4px; font-size: 20px; letter-spacing: -0.02em; }
.section-heading p { margin: 0; color: var(--fg-muted); font-size: 13px; }
.info-grid, .metric-grid, .detail-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.info-card, .metric-card { display: flex; flex-direction: column; gap: 6px; min-height: 108px; padding: 15px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }
.card-kicker { color: var(--fg-subtle); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
.info-card strong, .metric-card strong { font-size: 14px; }
.info-card p, .metric-card span, .feature-item p, .step p, .security-item p { margin: 0; color: var(--fg-muted); font-size: 12px; line-height: 1.65; }
.feature-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.feature-item { display: flex; gap: 11px; padding: 13px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }
.feature-icon { display: grid; flex: 0 0 26px; place-items: center; width: 26px; height: 26px; border-radius: 6px; background: var(--accent-weak); color: var(--accent-strong); font-size: 15px; }
.feature-item h3, .step h3, .detail-grid h3, .endpoint-card h3, .command-card h3 { margin: 0 0 3px; font-size: 13px; }
.steps { display: grid; gap: 8px; }
.step { display: flex; gap: 13px; padding: 13px 15px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }
.step-index { display: grid; flex: 0 0 23px; place-items: center; width: 23px; height: 23px; border-radius: 50%; background: var(--panel-3); color: var(--accent-strong); font-family: var(--mono); font-size: 11px; }
.callout { margin-top: 12px; padding: 14px 16px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }
.callout--accent { border-color: rgba(110,159,248,.34); background: var(--accent-weak); }
.callout strong { display: block; margin-bottom: 4px; font-size: 13px; }
.callout p { margin: 0; color: var(--fg-muted); font-size: 12px; line-height: 1.7; }
code, pre { font-family: var(--mono); }
code { color: var(--accent-strong); font-size: .92em; }
.plain-list { display: grid; gap: 9px; margin: 0; padding-left: 18px; color: var(--fg-muted); font-size: 13px; line-height: 1.65; }
.metric-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.metric-card { min-height: 82px; }
.detail-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.detail-grid > div { padding: 15px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }
.command-list { display: grid; gap: 10px; }
.command-card { overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }
.command-card__header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 14px 15px 10px; }
.command-card__header p { margin: 0; color: var(--fg-muted); font-size: 12px; }
.copy-button { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 5px; padding: 5px 8px; border: 1px solid var(--border-strong); border-radius: 5px; background: transparent; color: var(--fg-muted); cursor: pointer; font-size: 11px; }
.copy-button:hover { border-color: var(--accent); color: var(--fg); }
.command-card pre { overflow-x: auto; margin: 0; padding: 13px 15px 15px; border-top: 1px solid var(--border); background: #101310; color: var(--fg); font-size: 12px; line-height: 1.7; }
.endpoint-card { margin-top: 12px; padding: 15px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }
.endpoint-row { display: grid; grid-template-columns: 42px 1fr 1.4fr; gap: 10px; align-items: center; padding: 8px 0; border-top: 1px solid var(--border); font-size: 12px; }
.endpoint-row:first-of-type { border-top: 0; }
.endpoint-row code { color: var(--ok); }
.endpoint-row small { color: var(--fg-muted); }
.security-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
.security-item { padding: 14px; border-left: 2px solid var(--accent); background: var(--panel); }
.security-item strong { font-size: 13px; }
.faq-list { display: grid; gap: 7px; }
.faq-list details { padding: 12px 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel); }
.faq-list summary { cursor: pointer; font-size: 13px; font-weight: 600; }
.faq-list p { margin: 9px 0 0; color: var(--fg-muted); font-size: 12px; line-height: 1.7; }
.guide-index { position: sticky; top: 24px; display: flex; flex-direction: column; gap: 8px; padding-top: 38px; }
.guide-index__label { margin-bottom: 4px; color: var(--fg-subtle); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
.guide-index a { color: var(--fg-muted); font-size: 12px; text-decoration: none; }
.guide-index a:hover { color: var(--accent-strong); }
@media (max-width: 900px) {
  .guide-layout { grid-template-columns: 1fr; gap: 0; }
  .guide-index { position: static; order: -1; flex-direction: row; flex-wrap: wrap; padding: 14px 0 0; }
  .guide-index__label { flex-basis: 100%; }
}
@media (max-width: 680px) {
  .guide-page { padding: 24px 16px 40px; }
  .guide-hero { flex-direction: column; gap: 16px; }
  .hero-note { align-self: start; max-width: none; }
  .info-grid, .feature-list, .metric-grid, .detail-grid, .security-list { grid-template-columns: 1fr; }
  .endpoint-row { grid-template-columns: 42px 1fr; }
  .endpoint-row small { grid-column: 2; }
}
</style>
