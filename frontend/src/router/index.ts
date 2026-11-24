import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

const Dashboard = () => import("../views/Dashboard.vue");
const History = () => import("../views/History.vue");
const HistoryDetail = () => import("../views/HistoryDetail.vue");
const Memories = () => import("../views/Memories.vue");
const Audit = () => import("../views/Audit.vue");
const Insights = () => import("../views/Insights.vue");
const Projects = () => import("../views/Projects.vue");
const ProjectDetail = () => import("../views/ProjectDetail.vue");
const Guide = () => import("../views/Guide.vue");
const Storage = () => import("../views/Storage.vue");
const System = () => import("../views/System.vue");
const NotFound = () => import("../views/NotFound.vue");

const routes: RouteRecordRaw[] = [
  {
    path: "/",
    name: "dashboard",
    component: Dashboard,
    meta: { title: "Dashboard" },
  },
  { path: "/history", name: "history", component: History, meta: { title: "History" } },
  {
    path: "/memories",
    name: "memories",
    component: Memories,
    meta: { title: "Memories" },
  },
  {
    path: "/insights",
    name: "insights",
    component: Insights,
    meta: { title: "Insights" },
  },
  {
    path: "/projects",
    name: "projects",
    component: Projects,
    meta: { title: "Projects" },
  },
  {
    path: "/projects/:id",
    name: "project-detail",
    component: ProjectDetail,
    props: true,
    meta: { title: "Project" },
  },
  { path: "/guide", name: "guide", component: Guide, meta: { title: "使用方法" } },
  { path: "/stats", redirect: "/insights" },
  {
    path: "/history/:id",
    name: "history-detail",
    component: HistoryDetail,
    props: true,
    meta: { title: "History Session" },
  },
  { path: "/audit", name: "audit", component: Audit, meta: { title: "Audit" } },
  {
    path: "/storage",
    name: "storage",
    component: Storage,
    meta: { title: "Storage" },
  },
  {
    path: "/system",
    name: "system",
    component: System,
    meta: { title: "System" },
  },
  {
    path: "/:pathMatch(.*)*",
    name: "not-found",
    component: NotFound,
    meta: { title: "Not found" },
  },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.afterEach((to) => {
  document.title = `${(to.meta.title as string) ?? "Agent Observatory"} — Agent Observatory`;
});

export default router;
