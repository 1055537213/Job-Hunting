/**
 * Job Hunting Agent Vue 3 前端。
 *
 * 这一层只负责页面状态、用户交互和 SSE 展示：
 * - 结构化事实由后端 PostgreSQL 管理；
 * - 长文本仍由后端 long_texts/RAG 管理；
 * - 聊天通过 /api/chat/stream 接收 LangChain Agent 的增量输出；
 * - 页面不直接连接数据库，也不直接调用模型供应商 API。
 */

if (!window.Vue) {
  document.body.innerHTML =
    "<main class='boot-error'><h1>Vue 3 加载失败</h1><p>请确认本地静态资源完整，然后刷新页面。</p></main>";
} else {
  const { createApp, nextTick } = window.Vue;
  const DEFAULT_USE_LANGCHAIN_AGENT = true;
  const DEFAULT_AUTO_INCREMENTAL_RAG = true;
  // 模型或网络无响应时，前端最多等待三分钟；用户也可以提前停止生成。
  const CHAT_STREAM_TIMEOUT_MS = 180000;
  // 认证错误不会永久占据登录表单；用户输入新内容时会更早清除。
  const AUTH_ERROR_DISMISS_MS = 6000;
  const PINYIN_COLLATOR = new Intl.Collator("zh-CN-u-co-pinyin");

  /** 克隆并按拼音排列省份和城市，避免改变静态数据源。 */
  function buildSortedCityGroups() {
    const source = Array.isArray(window.CHINA_CITY_GROUPS) ? window.CHINA_CITY_GROUPS : [];
    return source
      .map((group) => ({
        province: group.province,
        cities: [...group.cities].sort(PINYIN_COLLATOR.compare),
      }))
      .sort((left, right) => PINYIN_COLLATOR.compare(left.province, right.province));
  }

  /** 统一网页城市值，确保“杭州市”和职位解析得到的“杭州”可以比较。 */
  function normalizeCityName(value) {
    const suffixes = ["特别行政区", "自治州", "地区", "盟", "市", "县"];
    let city = String(value || "").trim();
    const suffix = suffixes.find((item) => city.endsWith(item) && city.length > item.length);
    if (suffix) {
      city = city.slice(0, -suffix.length);
    }
    return city;
  }

  // 热门城市只作为快捷入口，完整城市目录仍由 china_cities.js 提供。
  const HOT_CITY_NAMES = [
    "北京市",
    "上海市",
    "广州市",
    "深圳市",
    "杭州市",
    "成都市",
    "南京市",
    "武汉市",
    "西安市",
    "苏州市",
    "重庆市",
    "天津市",
  ];

  const WELCOME_MESSAGE =
    "你好，我是求职助手 Agent，我能帮你整理个人经历、分析职位匹配度、完善简历并准备求职材料。\n请你先建立属于你的专属档案，我们就可以开始了。";

  createApp({
    data() {
      return {
        auth: {
          authenticated: false,
          account: null,
        },
        authMode: "login",
        authForm: {
          email: "",
          password: "",
          displayName: "",
        },
        authPasswordVisible: false,
        authLoading: false,
        authSuccess: false,
        authError: "",
        authErrorTimer: null,
        duplicateNotice: {
          open: false,
          title: "",
          message: "",
        },
        duplicateNoticeReturnTarget: null,
        jobImportNotice: {
          open: false,
          title: "",
          message: "",
        },
        jobImportNoticeReturnTarget: null,
        activeView: "workspace",
        workspaceRailOpen: false,
        activeWorkspacePanel: "",
        sessions: [],
        activeSessionId: "",
        sessionMenuOpen: false,
        admin: {
          accounts: [],
          events: [],
          toolTraces: [],
          selectedToolTraceId: "",
          toolTraceDetail: null,
          summary: {},
          requestMetrics: {},
          activeDetailTab: "tokens",
          selectedAccountId: 0,
          loadingEvents: false,
          loadingToolTraces: false,
          loadingToolTraceDetail: false,
          eventsError: "",
          toolTracesError: "",
          toolTraceDetailError: "",
          loadError: "",
          usageRequestVersion: 0,
          toolTraceRequestVersion: 0,
          toolTraceDetailRequestVersion: 0,
          toolTraceTotal: 0,
        },
        profiles: [],
        jobs: [],
        matches: [],
        resumeArtifacts: [],
        resumeJobSelections: {},
        projectCards: [],
        githubProjectUrl: "",
        messages: [],
        currentProfileId: Number(localStorage.getItem("currentProfileId") || 0),
        messageInput: "",
        cityGroups: buildSortedCityGroups(),
        cityPickerOpen: false,
        activeCityProvince: "hot",
        profileForm: {
          name: "",
          education: "",
          experienceYears: 0,
          skills: "",
          preferredCities: [],
          directions: "",
        },
        jobForm: {
          sourceUrl: "",
          rawText: "",
          screenshots: [],
        },
        jobImportMode: "text",
        health: {
          text: "检查中...",
          error: false,
          agentConfigured: false,
        },
        loadingProfiles: false,
        creatingProfile: false,
        deletingProfileId: 0,
        loadingJobs: false,
        deletingJobId: 0,
        importingJob: false,
        loadingMatches: false,
        savingJobSkillsId: 0,
        uploadingResume: false,
        tailoringArtifactId: 0,
        deletingResumeArtifactId: 0,
        resumeError: "",
        submittingGitHubProject: false,
        confirmingProjectCardId: 0,
        confirmingTaskApprovalId: 0,
        githubProjectError: "",
        // 当前页面正在跟踪的后台 RAG 任务；任务事实仍以 PostgreSQL API 返回值为准。
        backgroundTasks: {},
        ragTaskByArtifact: {},
        ragTaskPollers: {},
        ragTaskNotified: {},
        projectTaskPollers: {},
        projectTaskNotified: {},
        sending: false,
        chatAbortController: null,
        deletingSessionId: "",
        jobImportError: "",
        commandPaletteOpen: false,
        commandQuery: "",
        activeCommandIndex: 0,
        nextLocalMessageId: 0,
        messageScrollFrameId: null,
      };
    },

    computed: {
      /** 返回当前选中的候选人档案。 */
      currentProfile() {
        return this.profiles.find((profile) => profile.id === this.currentProfileId) || null;
      },

      /** 只有已选中的真实档案正在请求删除时，才显示“删除中”。 */
      isDeletingCurrentProfile() {
        return Boolean(this.currentProfileId) && this.deletingProfileId === this.currentProfileId;
      },

      /** 把结构化档案转换成工作台摘要框中的可读文本。 */
      profileSummary() {
        const profile = this.currentProfile;
        if (!profile) {
          return "请选择或创建候选人档案。";
        }
        return [
          `姓名：${profile.name}`,
          `状态：${profile.status}`,
          `学历：${profile.education}`,
          `经验：${profile.experience_years} 年`,
          `技能：${this.formatDict(profile.skills) || "暂无"}`,
          `首选城市：${profile.preferred_cities.join("、") || "暂无"}`,
          `其他可接受城市：${profile.acceptable_cities?.join("、") || "暂无"}`,
          `方向：${profile.target_directions.join("、") || "暂无"}`,
          `不可接受：${profile.unacceptable.join("、") || "暂无"}`,
        ].join("\n");
      },

      /** 当前档案的活动会话；首次聊天时会自动创建默认会话。 */
      currentSessionId() {
        return this.activeSessionId || `account-${this.auth.account?.id || "legacy"}-candidate-${this.currentProfileId}`;
      },

      /** 返回会话选择器当前显示的标题，避免原生 select 挤占聊天头部空间。 */
      currentSessionTitle() {
        return (
          this.sessions.find((session) => session.session_id === this.activeSessionId)?.title ||
          (this.sessions.length ? "选择会话" : "暂无会话")
        );
      },

      /** 管理后台当前展开的账号；未选择时不预先展示任何账号流水。 */
      selectedAdminAccount() {
        const selectedAccountId = Number(this.admin.selectedAccountId);
        return this.admin.accounts.find(
          (account) => Number(account.id) === selectedAccountId
        ) || null;
      },

      /** 当前选中的工具调用轨迹摘要。 */
      selectedAdminToolTrace() {
        const rootRequestId = String(this.admin.selectedToolTraceId || "");
        return this.admin.toolTraces.find(
          (trace) => String(trace.root_request_id || "") === rootRequestId
        ) || null;
      },

      /** 当前选中的工具调用完整详情。 */
      selectedAdminToolTraceDetail() {
        return this.admin.toolTraceDetail || this.selectedAdminToolTrace;
      },

      /** 当前城市选择面板右侧展示的一级地区。 */
      activeCityGroup() {
        return this.cityGroups.find((group) => group.province === this.activeCityProvince) || null;
      },

      /** 只保留目录中确实存在的热门城市，避免快捷入口与数据源漂移。 */
      hotCityOptions() {
        const availableCities = new Set(this.cityGroups.flatMap((group) => group.cities));
        return HOT_CITY_NAMES.filter((city) => availableCities.has(city));
      },

      /** 根据一级菜单返回二级城市；热门城市是一个独立的一级入口。 */
      activeCityOptions() {
        return this.activeCityProvince === "hot"
          ? this.hotCityOptions
          : this.activeCityGroup?.cities || [];
      },

      cityPickerTitle() {
        if (this.activeCityProvince === "hot") return "热门城市";
        return this.activeCityGroup?.province || "选择城市";
      },

      /** 命令面板展示的动作清单；只调用已有页面方法，不绕过后端接口边界。 */
      workspaceCommands() {
        return [
          {
            key: "chat",
            title: "补充候选人资料",
            description: this.currentProfileId
              ? "聚焦聊天输入框，发送经历、技能或项目材料。"
              : "先创建或选择候选人档案后再发送资料。",
            shortcut: "Ctrl Enter",
            action: "focusMessageInput",
            disabled: !this.currentProfileId,
          },
          {
            key: "job-import",
            title: "导入 BOSS 职位",
            description: "聚焦职位文本框，粘贴职位详情后由后端先审核再保存。",
            shortcut: "Paste",
            action: "focusJobImport",
            disabled: false,
          },
          {
            key: "resume-upload",
            title: "上传简历",
            description: "为当前候选人上传 DOCX 或 PDF 简历文件。",
            shortcut: "DOCX PDF",
            action: "triggerResumeUpload",
            disabled: !this.currentProfileId || this.uploadingResume,
          },
          {
            key: "github-project",
            title: "分析 GitHub 项目",
            description: "提交当前候选人的公开仓库链接并生成待确认项目卡片。",
            shortcut: "GitHub",
            action: "focusGitHubProject",
            disabled: !this.currentProfileId || this.submittingGitHubProject,
          },
          {
            key: "match",
            title: "匹配当前候选人",
            description: "按学历、年限、技能和偏好规则重算职位排序。",
            shortcut: "Run",
            action: "matchJobs",
            disabled: !this.currentProfileId || this.loadingMatches,
          },
          {
            key: "refresh-jobs",
            title: "刷新职位列表",
            description: "重新读取 PostgreSQL 中已导入且通过审核的职位。",
            shortcut: "Sync",
            action: "loadJobs",
            disabled: this.loadingJobs,
          },
        ];
      },

      /** 根据用户输入过滤命令；空查询时展示全部常用动作。 */
      filteredCommands() {
        const query = this.commandQuery.toLowerCase();
        if (!query) {
          return this.workspaceCommands;
        }
        return this.workspaceCommands.filter((item) =>
          `${item.title} ${item.description} ${item.shortcut}`.toLowerCase().includes(query)
        );
      },
    },

    watch: {
      /** 登录状态变化时同步根容器布局，登录页才能真正使用整屏背景。 */
      "auth.authenticated": {
          immediate: true,
          handler(authenticated) {
            this.syncAuthPageClass(authenticated);
            if (!authenticated) {
              this.setWorkspaceRailOpen(false);
              this.activeWorkspacePanel = "";
            }
          },
        },

      /** 错误发生在折叠菜单内时，自动打开对应功能，避免错误被隐藏。 */
      resumeError(value) {
        if (value) this.openWorkspacePanel("resume");
      },
      githubProjectError(value) {
        if (value) this.openWorkspacePanel("github");
      },
      jobImportError(value) {
        if (value) this.openWorkspacePanel("job-import");
      },

      /** 查询变化后重置高亮项，避免键盘选择停在不存在的结果上。 */
      commandQuery() {
        this.activeCommandIndex = 0;
      },
    },

    mounted() {
      this.syncAuthPageClass(this.auth.authenticated);
      this.checkAuth();
      document.addEventListener("keydown", this.handleGlobalShortcut);
    },

    beforeUnmount() {
      document.removeEventListener("keydown", this.handleGlobalShortcut);
      document.body.classList.remove("cmdk-lock");
      document.body.classList.remove("workspace-rail-lock");
      document.body.classList.remove("duplicate-dialog-lock");
      document.body.classList.remove("job-import-dialog-lock");
      if (this.messageScrollFrameId !== null) {
        if (window.requestAnimationFrame && window.cancelAnimationFrame) {
          window.cancelAnimationFrame(this.messageScrollFrameId);
        } else {
          window.clearTimeout(this.messageScrollFrameId);
        }
        this.messageScrollFrameId = null;
      }
      this.clearAuthFeedback();
      Object.values(this.ragTaskPollers).forEach((timerId) => window.clearTimeout(timerId));
      this.ragTaskPollers = {};
      Object.values(this.projectTaskPollers).forEach((timerId) => window.clearTimeout(timerId));
      this.projectTaskPollers = {};
    },

    methods: {
      /** 给 Vue 挂载容器切换登录页全宽布局；工作台恢复原有边距和最大宽度。 */
      syncAuthPageClass(authenticated) {
        document.getElementById("app")?.classList.toggle("auth-page", !authenticated);
      },

      /** 先读取服务端 Session；未登录时不请求任何候选人或职位数据。 */
      async checkAuth() {
        try {
          const data = await this.requestJson("/api/auth/me");
          this.auth.authenticated = Boolean(data.authenticated);
          this.auth.account = data.account || null;
          if (this.auth.authenticated) {
            await this.initialize();
          }
        } catch (error) {
          this.auth.authenticated = false;
          this.auth.account = null;
          // 初始化探测失败不等同于登录失败，避免刷新页面时提前显示错误框。
        }
      },

      /** 切换登录与注册表单。 */
      toggleAuthMode() {
        this.authMode = this.authMode === "login" ? "register" : "login";
        this.clearAuthFeedback();
        this.authForm.password = "";
        this.authPasswordVisible = false;
      },

      /** 切换登录密码的显示状态；注册模式始终直接显示密码。 */
      toggleAuthPassword() {
        if (this.authMode === "login") {
          this.authPasswordVisible = !this.authPasswordVisible;
        }
      },

      /** 清理认证提示和旧定时器，避免错误信息覆盖用户下一次输入。 */
      clearAuthFeedback() {
        if (this.authErrorTimer !== null) {
          window.clearTimeout(this.authErrorTimer);
          this.authErrorTimer = null;
        }
        this.authError = "";
        this.authSuccess = false;
      },

      /** 显示认证错误，并在用户没有继续操作时自动隐藏。 */
      showAuthError(message) {
        this.clearAuthFeedback();
        this.authError = message || "认证请求失败。";
        this.authErrorTimer = window.setTimeout(() => {
          this.clearAuthFeedback();
        }, AUTH_ERROR_DISMISS_MS);
      },

      /** 409 表示同一账号中已经存在相同内容；用统一居中提示而非表单内错误。 */
      showDuplicateNotice(error, title) {
        if (Number(error?.status) !== 409) {
          return false;
        }
        this.duplicateNoticeReturnTarget =
          document.activeElement instanceof HTMLElement ? document.activeElement : null;
        this.duplicateNotice = {
          open: true,
          title: title || "内容已存在",
          message: error.message || "相同内容已存在，未重复保存。",
        };
        document.body.classList.add("duplicate-dialog-lock");
        nextTick(() => this.$refs.duplicateDialogClose?.focus());
        return true;
      },

      /** 关闭重复提示并尽量把焦点还给触发操作的控件。 */
      closeDuplicateNotice() {
        const returnTarget = this.duplicateNoticeReturnTarget;
        this.duplicateNotice.open = false;
        this.duplicateNoticeReturnTarget = null;
        document.body.classList.remove("duplicate-dialog-lock");
        nextTick(() => returnTarget?.focus?.());
      },

      /** 显示截图审核未通过的居中提示，避免用户在折叠面板中遗漏导入失败原因。 */
      showJobImportNotice(message, title = "无法导入职位截图") {
        this.jobImportNoticeReturnTarget =
          document.activeElement instanceof HTMLElement ? document.activeElement : null;
        this.jobImportNotice = {
          open: true,
          title,
          message: message || "职位截图未通过审核，请换一张更完整的截图后重试。",
        };
        document.body.classList.add("job-import-dialog-lock");
        nextTick(() => this.$refs.jobImportNoticeClose?.focus());
      },

      /** 关闭截图审核提示，并将焦点还给触发导入的控件。 */
      closeJobImportNotice() {
        const returnTarget = this.jobImportNoticeReturnTarget;
        this.jobImportNotice.open = false;
        this.jobImportNoticeReturnTarget = null;
        document.body.classList.remove("job-import-dialog-lock");
        nextTick(() => returnTarget?.focus?.());
      },

      /** 提交登录或普通用户注册。 */
      async submitAuth() {
        this.authLoading = true;
        this.clearAuthFeedback();
        try {
          const endpoint = this.authMode === "login" ? "/api/auth/login" : "/api/auth/register";
          const data = await this.requestJson(endpoint, {
            method: "POST",
            body: JSON.stringify({
              email: this.authForm.email,
              password: this.authForm.password,
              display_name: this.authForm.displayName || null,
            }),
          });
          if (this.authMode === "register") {
            this.authMode = "login";
            this.authSuccess = true;
            this.authError = "账号已创建，请登录。";
            this.authForm.password = "";
            this.authPasswordVisible = false;
            return;
          }
          this.auth.authenticated = true;
          this.auth.account = data.account || null;
          this.activeView = "workspace";
          this.authForm.password = "";
          this.authPasswordVisible = false;
          await this.initialize();
          await nextTick();
          document.querySelector("#chatPanel")?.focus?.();
        } catch (error) {
          if (this.showDuplicateNotice(error, "账号已存在")) {
            return;
          }
          this.showAuthError(error.message || "认证请求失败。");
        } finally {
          this.authLoading = false;
        }
      },

      /** 注销当前设备；服务端会撤销 Session 并清理 Cookie。 */
      async logout() {
        try {
          await this.requestJson("/api/auth/logout", { method: "POST" });
        } catch (error) {
          this.showAuthError(error.message || "退出失败。");
        }
        this.auth.authenticated = false;
        this.auth.account = null;
        this.activeView = "workspace";
        this.messages = [];
        this.profiles = [];
        this.jobs = [];
        this.matches = [];
        this.resumeArtifacts = [];
        this.resumeJobSelections = {};
      },

      /** 撤销当前账号在所有设备上的 Session，并回到登录页。 */
      async logoutAll() {
        try {
          await this.requestJson("/api/auth/logout-all", { method: "POST" });
        } catch (error) {
          this.showAuthError(error.message || "退出所有设备失败。");
        }
        this.auth.authenticated = false;
        this.auth.account = null;
        this.activeView = "workspace";
        this.messages = [];
        this.profiles = [];
        this.jobs = [];
        this.matches = [];
        this.resumeArtifacts = [];
        this.resumeJobSelections = {};
        this.sessions = [];
        this.activeSessionId = "";
      },

      /** 打开管理员用量页面并刷新脱敏后台数据。 */
      async openAdmin() {
        if (this.auth.account?.role !== "admin") {
          return;
        }
        this.activeView = "admin";
        await this.loadAdminData();
      },

      /** 加载账号列表和汇总；Token 明细在管理员选择账号后按账号请求。 */
      async loadAdminData() {
        try {
          const [accounts, summary, requestMetrics] = await Promise.all([
            this.requestJson("/api/admin/accounts"),
            this.requestJson("/api/admin/usage/summary"),
            this.requestJson("/api/admin/observability/requests"),
          ]);
          this.admin.accounts = accounts.accounts || [];
          this.admin.summary = {
            ...(summary.summary || {}),
            by_account: summary.by_account || [],
            tool_calls_by_account: summary.tool_calls_by_account || [],
          };
          this.admin.requestMetrics = requestMetrics.requests || {};
          this.admin.loadError = "";

          const selectedAccountId = Number(this.admin.selectedAccountId);
          const selectedAccountStillExists = this.admin.accounts.some(
            (account) => Number(account.id) === selectedAccountId
          );
          if (!selectedAccountStillExists) {
            this.clearAdminUsageSelection();
            return;
          }
          await this.loadAdminActiveDetail(selectedAccountId);
        } catch (error) {
          this.admin.loadError = error.message || "后台数据加载失败，请稍后重试。";
        }
      },

      /** 点击左侧账号项后才读取右侧的 Token 明细；已选账号保持展开状态。 */
      async selectAdminAccount(accountId) {
        const selectedAccountId = Number(accountId);
        if (!Number.isInteger(selectedAccountId) || selectedAccountId <= 0) return;
        if (this.isAdminAccountSelected(selectedAccountId)) {
          return;
        }
        this.admin.selectedAccountId = selectedAccountId;
        this.admin.events = [];
        this.admin.toolTraces = [];
        this.admin.selectedToolTraceId = "";
        this.admin.toolTraceDetail = null;
        this.admin.eventsError = "";
        this.admin.toolTracesError = "";
        this.admin.toolTraceDetailError = "";
        await this.loadAdminActiveDetail(selectedAccountId);
      },

      /** 返回账号是否为当前展开的一级项。 */
      isAdminAccountSelected(accountId) {
        return Number(this.admin.selectedAccountId) === Number(accountId);
      },

      /** 清空已展开账号及其二级流水，并让先前未完成的请求失效。 */
      clearAdminUsageSelection() {
        this.admin.usageRequestVersion += 1;
        this.admin.toolTraceRequestVersion += 1;
        this.admin.toolTraceDetailRequestVersion += 1;
        this.admin.selectedAccountId = 0;
        this.admin.events = [];
        this.admin.toolTraces = [];
        this.admin.selectedToolTraceId = "";
        this.admin.toolTraceDetail = null;
        this.admin.eventsError = "";
        this.admin.toolTracesError = "";
        this.admin.toolTraceDetailError = "";
        this.admin.loadingEvents = false;
        this.admin.loadingToolTraces = false;
        this.admin.loadingToolTraceDetail = false;
        this.admin.toolTraceTotal = 0;
      },

      /** 根据当前标签加载账号右侧明细。 */
      async loadAdminActiveDetail(accountId = this.admin.selectedAccountId) {
        if (this.admin.activeDetailTab === "tools") {
          await this.loadAdminToolTraces(accountId);
        } else {
          await this.loadAdminUsageEvents(accountId);
        }
      },

      /** 切换账号详情标签；只加载当前用户正在看的数据。 */
      async setAdminDetailTab(tab) {
        if (!["tokens", "tools"].includes(tab)) return;
        if (this.admin.activeDetailTab === tab) return;
        this.admin.activeDetailTab = tab;
        await this.loadAdminActiveDetail();
      },

      /** 使用已有 account_id 查询参数读取单个账号的最多 200 条最新流水。 */
      async loadAdminUsageEvents(accountId = this.admin.selectedAccountId) {
        const selectedAccountId = Number(accountId);
        if (!Number.isInteger(selectedAccountId) || selectedAccountId <= 0) return;

        const requestVersion = this.admin.usageRequestVersion + 1;
        this.admin.usageRequestVersion = requestVersion;
        this.admin.loadingEvents = true;
        this.admin.eventsError = "";
        try {
          const data = await this.requestJson(
            `/api/admin/usage/events?account_id=${encodeURIComponent(selectedAccountId)}&limit=200`
          );
          if (
            requestVersion !== this.admin.usageRequestVersion ||
            !this.isAdminAccountSelected(selectedAccountId)
          ) {
            return;
          }
          this.admin.events = data.events || [];
        } catch (error) {
          if (
            requestVersion !== this.admin.usageRequestVersion ||
            !this.isAdminAccountSelected(selectedAccountId)
          ) {
            return;
          }
          this.admin.events = [];
          this.admin.eventsError = error.message || "Token 明细加载失败，请稍后重试。";
        } finally {
          if (requestVersion === this.admin.usageRequestVersion) {
            this.admin.loadingEvents = false;
          }
        }
      },

      /** 按账号分页读取最近两天内的工具调用任务摘要。 */
      async loadAdminToolTraces(accountId = this.admin.selectedAccountId, offset = 0) {
        const selectedAccountId = Number(accountId);
        if (!Number.isInteger(selectedAccountId) || selectedAccountId <= 0) return;

        const requestVersion = this.admin.toolTraceRequestVersion + 1;
        this.admin.toolTraceRequestVersion = requestVersion;
        this.admin.loadingToolTraces = true;
        this.admin.toolTracesError = "";
        try {
          const data = await this.requestJson(
            `/api/admin/tools/traces?account_id=${encodeURIComponent(selectedAccountId)}&limit=50&offset=${encodeURIComponent(offset)}`
          );
          if (
            requestVersion !== this.admin.toolTraceRequestVersion ||
            !this.isAdminAccountSelected(selectedAccountId)
          ) {
            return;
          }
          this.admin.toolTraces = data.traces || [];
          this.admin.toolTraceTotal = Number(data.total || 0);
          const selectedStillExists = this.admin.toolTraces.some(
            (trace) => String(trace.root_request_id || "") === String(this.admin.selectedToolTraceId || "")
          );
          if (!selectedStillExists) {
            this.admin.selectedToolTraceId = "";
            this.admin.toolTraceDetail = null;
          }
        } catch (error) {
          if (
            requestVersion !== this.admin.toolTraceRequestVersion ||
            !this.isAdminAccountSelected(selectedAccountId)
          ) {
            return;
          }
          this.admin.toolTraces = [];
          this.admin.toolTraceTotal = 0;
          this.admin.toolTracesError = error.message || "工具调用记录加载失败，请稍后重试。";
        } finally {
          if (requestVersion === this.admin.toolTraceRequestVersion) {
            this.admin.loadingToolTraces = false;
          }
        }
      },

      /** 点击任务后按需读取完整工具调用流程。 */
      async selectAdminToolTrace(rootRequestId) {
        const traceId = String(rootRequestId || "");
        if (!traceId) return;
        if (this.admin.selectedToolTraceId === traceId && this.admin.toolTraceDetail) return;

        const requestVersion = this.admin.toolTraceDetailRequestVersion + 1;
        this.admin.toolTraceDetailRequestVersion = requestVersion;
        this.admin.selectedToolTraceId = traceId;
        this.admin.toolTraceDetail = null;
        this.admin.loadingToolTraceDetail = true;
        this.admin.toolTraceDetailError = "";
        try {
          const data = await this.requestJson(`/api/admin/tools/traces/${encodeURIComponent(traceId)}`);
          if (
            requestVersion !== this.admin.toolTraceDetailRequestVersion ||
            this.admin.selectedToolTraceId !== traceId
          ) {
            return;
          }
          this.admin.toolTraceDetail = data.trace || null;
        } catch (error) {
          if (
            requestVersion !== this.admin.toolTraceDetailRequestVersion ||
            this.admin.selectedToolTraceId !== traceId
          ) {
            return;
          }
          this.admin.toolTraceDetailError = error.message || "工具调用详情加载失败，请稍后重试。";
        } finally {
          if (requestVersion === this.admin.toolTraceDetailRequestVersion) {
            this.admin.loadingToolTraceDetail = false;
          }
        }
      },

      /** 管理员切换普通账号启用状态。 */
      async toggleAccountStatus(account) {
        const nextStatus = account.status === "active" ? "disabled" : "active";
        try {
          await this.requestJson(`/api/admin/accounts/${account.id}/status`, {
            method: "PATCH",
            body: JSON.stringify({ status: nextStatus }),
          });
          await this.loadAdminData();
        } catch (error) {
          this.appendAssistant(`账号状态更新失败：${error.message || "未知错误"}`, true);
        }
      },

      /** 格式化后台时间，避免把原始 ISO 字符串塞进密集表格。 */
      formatDate(value) {
        if (!value) return "-";
        return String(value).replace("T", " ").replace(/\+00:00$/, "");
      },

      /** 读取管理员返回的账号级可计费用量。 */
      accountUsage(accountId) {
        const item = this.accountUsageSummary(accountId);
        return item?.billable_tokens || 0;
      },

      /** 返回指定账号的汇总行，供一级账号项显示 Token 和流水数量。 */
      accountUsageSummary(accountId) {
        return (this.admin.summary.by_account || []).find(
          (entry) => Number(entry.account_id) === Number(accountId)
        ) || null;
      },

      /** 返回指定账号已有的用量流水条数。 */
      accountUsageEventCount(accountId) {
        return this.accountUsageSummary(accountId)?.event_count || 0;
      },

      /** 返回指定账号的工具调用任务统计。 */
      accountToolCallSummary(accountId) {
        return (this.admin.summary.tool_calls_by_account || []).find(
          (entry) => Number(entry.account_id) === Number(accountId)
        ) || null;
      },

      /** 返回指定账号的工具调用任务总数。 */
      accountToolCallCount(accountId) {
        return this.accountToolCallSummary(accountId)?.trace_count || 0;
      },

      /** 返回指定账号的工具调用失败次数。 */
      accountToolCallFailureCount(accountId) {
        return this.accountToolCallSummary(accountId)?.failed_trace_count || 0;
      },

      /** 监听全局 Ctrl/Cmd+K 和 Esc，提供类似工作台的快速动作入口。 */
      handleGlobalShortcut(event) {
        // 键盘事件可能由扩展、自动化脚本或其他页面代码转发；缺少 key 时直接忽略。
        if (!event || typeof event.key !== "string") {
          return;
        }
        if (this.duplicateNotice.open) {
          if (event.key === "Escape") {
            event.preventDefault();
            this.closeDuplicateNotice();
          }
          return;
        }
        if (this.jobImportNotice.open) {
          if (event.key === "Escape") {
            event.preventDefault();
            this.closeJobImportNotice();
          }
          return;
        }
        const key = event.key.toLowerCase();
        if ((event.ctrlKey || event.metaKey) && key === "k") {
          event.preventDefault();
          if (this.commandPaletteOpen) {
            this.closeCommandPalette();
          } else {
            this.openCommandPalette();
          }
        } else if (event.key === "Escape") {
          if (this.cityPickerOpen) {
            this.closeCityPicker();
          } else if (this.sessionMenuOpen) {
            this.closeSessionMenu();
          } else if (this.commandPaletteOpen) {
            this.closeCommandPalette();
          } else if (this.workspaceRailOpen) {
            this.closeWorkspaceRail();
          }
        }
      },

      /** 切换左侧工作台功能抽屉；桌面端同样保留状态，方便断点切换。 */
      toggleWorkspaceRail() {
        this.setWorkspaceRailOpen(!this.workspaceRailOpen);
      },

      /** 关闭窄屏工作台抽屉。 */
      closeWorkspaceRail() {
        this.setWorkspaceRailOpen(false);
      },

      /** 移动端抽屉打开时锁定页面滚动，关闭时恢复原有滚动行为。 */
      setWorkspaceRailOpen(isOpen) {
        this.workspaceRailOpen = Boolean(isOpen);
        document.body?.classList.toggle("workspace-rail-lock", this.workspaceRailOpen);
      },

      /** 单开工作台功能；正文使用 v-show 保留用户已填写的表单状态。 */
      toggleWorkspacePanel(panelKey) {
        this.activeWorkspacePanel = this.activeWorkspacePanel === panelKey ? "" : panelKey;
      },

      /** 由命令面板或异步错误调用，先打开目标功能再交给 nextTick 聚焦。 */
      openWorkspacePanel(panelKey) {
        this.activeWorkspacePanel = panelKey;
        if (window.matchMedia?.("(max-width: 60rem)").matches) {
          this.setWorkspaceRailOpen(true);
        }
      },

      /** 展开或收起当前档案的会话菜单。 */
      toggleSessionMenu() {
        if (!this.currentProfileId || !this.sessions.length) {
          return;
        }
        this.sessionMenuOpen = !this.sessionMenuOpen;
      },

      /** 关闭会话菜单；根节点点击和 Escape 都会调用此方法。 */
      closeSessionMenu() {
        this.sessionMenuOpen = false;
      },

      /** 打开城市两级菜单，并在首次打开时定位到热门城市。 */
      openCityPicker() {
        this.cityPickerOpen = true;
        if (!this.activeCityGroup && this.activeCityProvince !== "hot") {
          this.activeCityProvince = "hot";
        }
      },

      /** 关闭城市菜单，不影响已选择的首选城市。 */
      closeCityPicker() {
        this.cityPickerOpen = false;
      },

      /** 将城市菜单滚轮锁定在菜单内部，避免滚动链带动左侧档案栏。 */
      handleCityPickerWheel(event) {
        event.preventDefault();
        event.stopPropagation();

        const menu = event.currentTarget;
        if (!menu || typeof menu.querySelector !== "function") return;

        const target = event.target && typeof event.target.closest === "function"
          ? event.target
          : null;
        const primary = menu.querySelector(".city-picker-primary");
        const cityList = menu.querySelector(".city-picker-city-list");
        const scroller = target?.closest(".city-picker-primary")
          ? primary
          : target?.closest(".city-picker-city-list")
            ? cityList
            : cityList;
        if (!scroller) return;

        const delta = Number(event.deltaY) || 0;
        if (!delta) return;
        const unit = event.deltaMode === 1
          ? 16
          : event.deltaMode === 2
            ? scroller.clientHeight
            : 1;
        scroller.scrollTop += delta * unit;
      },

      /** 切换城市选择器的一级地区，不在这一步写入档案。 */
      selectCityProvince(province) {
        this.activeCityProvince = province;
      },

      /** 打开命令面板，并把焦点交给搜索输入框。 */
      openCommandPalette() {
        this.commandPaletteOpen = true;
        this.commandQuery = "";
        this.activeCommandIndex = 0;
        document.body.classList.add("cmdk-lock");
        nextTick(() => {
          this.$refs.commandInput?.focus();
        });
      },

      /** 关闭命令面板，同时恢复页面滚动。 */
      closeCommandPalette() {
        this.commandPaletteOpen = false;
        document.body.classList.remove("cmdk-lock");
      },

      /** 用方向键移动命令面板中的高亮项。 */
      moveCommandSelection(offset) {
        const total = this.filteredCommands.length;
        if (!total) {
          return;
        }
        this.activeCommandIndex = (this.activeCommandIndex + offset + total) % total;
      },

      /** 执行当前高亮命令。 */
      runActiveCommand() {
        const total = this.filteredCommands.length;
        if (!total) {
          return;
        }
        const index = Math.min(this.activeCommandIndex, total - 1);
        this.runCommand(this.filteredCommands[index]);
      },

      /** 执行指定命令；命令只分发到本页面已有方法，避免产生隐藏副作用。 */
      async runCommand(item) {
        if (!item || item.disabled) {
          return;
        }
        this.closeCommandPalette();
        await nextTick();
        const action = this[item.action];
        if (typeof action === "function") {
          await action.call(this);
        }
      },

      /** 聚焦聊天输入框，让用户继续补充候选人资料。 */
      focusMessageInput() {
        if (!this.currentProfileId) {
          this.appendAssistant("请先创建或选择候选人档案，再补充资料。", true);
          return;
        }
        nextTick(() => {
          this.$refs.messageInput?.focus();
        });
      },

      /** 聚焦职位导入框，便于从 BOSS 复制职位详情后直接粘贴。 */
      focusJobImport() {
        this.openWorkspacePanel("job-import");
        nextTick(() => {
          if (this.jobImportMode === "screenshot") {
            this.$refs.jobScreenshotFiles?.focus();
            return;
          }
          this.$refs.jobText?.focus();
        });
      },

      /** 打开当前候选人的本地 DOCX/PDF 文件选择器。 */
      triggerResumeUpload() {
        this.openWorkspacePanel("resume");
        if (!this.currentProfileId) {
          this.appendAssistant("请先创建或选择候选人档案，再上传简历。", true);
          return;
        }
        this.$refs.resumeFileInput?.click();
      },

      /** 聚焦公开 GitHub 仓库链接输入框。 */
      focusGitHubProject() {
        this.openWorkspacePanel("github");
        if (!this.currentProfileId) {
          this.appendAssistant("请先创建或选择候选人档案，再分析 GitHub 项目。", true);
          return;
        }
        nextTick(() => {
          this.$refs.githubProjectUrl?.focus();
        });
      },

      /**
       * 初始化页面所需数据。
       *
       * 顺序上先检查服务，再恢复候选人和职位列表；
       * 档案加载完成后会继续恢复聊天历史和匹配结果。
       */
      async initialize() {
        await this.loadHealth();
        await this.loadProfiles();
        await this.loadJobs();
      },

      /** 读取后端健康状态；聊天请求始终默认走 Agent + 自动增量 RAG。 */
      async loadHealth() {
        try {
          const data = await this.requestJson("/api/health");
          const agentText = data.agent?.configured ? "Agent 已就绪" : "Agent 未启用";
          const llmText = data.llm?.configured ? "LLM 已配置" : "LLM 未配置";
          const embeddingText = data.embedding?.configured ? "Embedding 真实" : "Embedding 本地";
          const rerankText = data.rerank?.configured ? "Rerank 已启用" : "Rerank 未启用";
          this.health = {
            text: `${agentText} · ${llmText} · ${embeddingText} · ${rerankText}`,
            error: false,
            agentConfigured: Boolean(data.agent?.configured),
          };
        } catch (error) {
          this.health = {
            text: "服务异常",
            error: true,
            agentConfigured: false,
          };
        }
      },

      /** 从后端恢复候选人档案，并选择上一次使用的档案。 */
      async loadProfiles() {
        this.loadingProfiles = true;
        try {
          const data = await this.requestJson("/api/profiles");
          this.profiles = data.profiles || [];
          if (!this.profiles.length) {
            this.currentProfileId = 0;
            localStorage.removeItem("currentProfileId");
            this.setWelcomeMessage();
            this.matches = [];
            this.resumeArtifacts = [];
            this.projectCards = [];
            return;
          }

          if (!this.profiles.some((profile) => profile.id === this.currentProfileId)) {
            this.currentProfileId = this.profiles[0].id;
          }
          localStorage.setItem("currentProfileId", String(this.currentProfileId));
          await this.refreshCurrentProfile();
          await this.loadChatSessions();
          await this.loadChatHistory();
          await this.loadResumeArtifacts();
          await this.loadProjectCards();
          this.resumePendingProjectTasks();
          await this.matchJobs(true);
        } finally {
          this.loadingProfiles = false;
        }
      },

      /** 用户切换档案后的联动刷新。 */
      async onProfileChange() {
        localStorage.setItem("currentProfileId", String(this.currentProfileId));
        if (!this.currentProfileId) {
          this.setWelcomeMessage();
          this.matches = [];
          this.resumeArtifacts = [];
          this.projectCards = [];
          this.resumeJobSelections = {};
          return;
        }
        await this.refreshCurrentProfile();
        await this.loadChatSessions();
        await this.loadChatHistory();
        await this.loadResumeArtifacts();
        await this.loadProjectCards();
        this.resumePendingProjectTasks();
        await this.matchJobs(true);
      },

      /** 读取当前档案的会话索引；会话内容仍由单独 history 接口恢复。 */
      async loadChatSessions() {
        if (!this.currentProfileId) {
          this.sessions = [];
          this.activeSessionId = "";
          this.closeSessionMenu();
          return;
        }
        const data = await this.requestJson(`/api/chat/sessions?candidate_id=${this.currentProfileId}`);
        this.sessions = data.sessions || [];
        const saved = localStorage.getItem(`activeSessionId:${this.auth.account?.id || "legacy"}:${this.currentProfileId}`);
        this.activeSessionId = this.sessions.some((item) => item.session_id === saved)
          ? saved
          : this.sessions[0]?.session_id || "";
        if (this.activeSessionId) {
          localStorage.setItem(`activeSessionId:${this.auth.account?.id || "legacy"}:${this.currentProfileId}`, this.activeSessionId);
        }
        if (!this.sessions.length) {
          this.closeSessionMenu();
        }
      },

      /** 新建一个不继承旧对话记忆的会话。 */
      async createChatSession() {
        if (!this.currentProfileId) return;
        try {
          const data = await this.requestJson("/api/chat/sessions", {
            method: "POST",
            body: JSON.stringify({ candidate_id: this.currentProfileId, title: "新求职对话" }),
          });
          this.activeSessionId = data.session.session_id;
          this.sessions = [data.session, ...this.sessions];
          this.closeSessionMenu();
          localStorage.setItem(`activeSessionId:${this.auth.account?.id}:${this.currentProfileId}`, this.activeSessionId);
          this.setWelcomeMessage();
        } catch (error) {
          this.appendAssistant(`新建对话失败：${error.message || "未知错误"}`, true);
        }
      },

      /** 切换会话时只恢复该会话的消息，不修改档案事实。 */
      async switchChatSession() {
        localStorage.setItem(`activeSessionId:${this.auth.account?.id || "legacy"}:${this.currentProfileId}`, this.activeSessionId);
        await this.loadChatHistory();
      },

      /** 从会话列表选择一段对话并恢复其消息。 */
      async selectChatSession(sessionId) {
        this.activeSessionId = sessionId;
        this.closeSessionMenu();
        await this.switchChatSession();
      },

      /** 删除会话列表中的指定对话，删除当前会话时同步清空消息区。 */
      async deleteSession(session) {
        if (!session || !window.confirm(`确定删除“${session.title || "当前对话"}”吗？\n该对话的历史消息将被永久删除。`)) {
          return;
        }
        this.closeSessionMenu();
        const sessionId = session.session_id;
        this.deletingSessionId = sessionId;
        try {
          await this.requestJson(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
          const wasActive = this.activeSessionId === sessionId;
          localStorage.removeItem(`activeSessionId:${this.auth.account?.id || "legacy"}:${this.currentProfileId}`);
          if (wasActive) {
            this.activeSessionId = "";
            this.messages = [];
          }
          await this.loadChatSessions();
          if (wasActive) {
            await this.loadChatHistory();
          }
        } catch (error) {
          this.appendAssistant(`删除对话失败：${error.message || "未知错误"}`, true);
        } finally {
          this.deletingSessionId = "";
        }
      },

      /** 永久删除当前对话及其消息，不影响候选人档案事实和计费流水。 */
      async deleteCurrentSession() {
        if (!this.activeSessionId) return;
        const session = this.sessions.find((item) => item.session_id === this.activeSessionId);
        const title = session?.title || "当前对话";
        if (!window.confirm(`确定删除“${title}”吗？\n该对话的历史消息将被永久删除。`)) {
          return;
        }
        const sessionId = this.activeSessionId;
        this.deletingSessionId = sessionId;
        try {
          await this.requestJson(`/api/chat/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
          localStorage.removeItem(`activeSessionId:${this.auth.account?.id || "legacy"}:${this.currentProfileId}`);
          this.activeSessionId = "";
          this.messages = [];
          await this.loadChatSessions();
          await this.loadChatHistory();
        } catch (error) {
          this.appendAssistant(`删除对话失败：${error.message || "未知错误"}`, true);
        } finally {
          this.deletingSessionId = "";
        }
      },

      /** 创建候选人档案并自动切换到新档案。 */
      async createProfile() {
        if (!this.profileForm.name.trim()) {
          this.appendAssistant("请先填写候选人姓名。", true);
          return;
        }

        this.creatingProfile = true;
        try {
          const payload = {
            name: this.profileForm.name.trim(),
            status: "待补充",
            education: this.profileForm.education.trim() || "待补充",
            experience_years: Number(this.profileForm.experienceYears || 0),
            skills: this.parseSkills(this.profileForm.skills),
            preferred_cities: [...this.profileForm.preferredCities],
            acceptable_cities: [],
            salary_floor_k: null,
            expected_salary_k: null,
            target_directions: this.splitItems(this.profileForm.directions),
            unacceptable: [],
          };
          const data = await this.requestJson("/api/profiles", {
            method: "POST",
            body: JSON.stringify(payload),
          });
          this.currentProfileId = data.candidate_id;
          localStorage.setItem("currentProfileId", String(this.currentProfileId));
          await this.loadProfiles();
          this.appendAssistant(`已创建候选人档案：${data.profile.name}。现在可以直接发送资料。`);
          this.profileForm = {
            name: "",
            education: "",
            experienceYears: 0,
            skills: "",
            preferredCities: [],
            directions: "",
          };
          this.closeCityPicker();
          this.activeCityProvince = "hot";
        } catch (error) {
          if (this.showDuplicateNotice(error, "候选人档案已存在")) {
            return;
          }
          this.appendAssistant(error.message, true);
        } finally {
          this.creatingProfile = false;
        }
      },

      /** 把两级菜单中选中的城市加入首选列表。 */
      addPreferredCity(cityValue = "") {
        const city = normalizeCityName(cityValue);
        if (city && !this.profileForm.preferredCities.includes(city)) {
          this.profileForm.preferredCities.push(city);
        }
      },

      /** 返回城市是否已经加入首选列表，目录值与存储值按同一规则比较。 */
      isPreferredCity(cityValue) {
        return this.profileForm.preferredCities.includes(normalizeCityName(cityValue));
      },

      /** 点击二级城市时在首选列表中切换，便于连续完成多城市选择。 */
      togglePreferredCity(cityValue) {
        const city = normalizeCityName(cityValue);
        if (!city) return;
        if (this.profileForm.preferredCities.includes(city)) {
          this.removePreferredCity(city);
          return;
        }
        this.addPreferredCity(city);
      },

      /** 从新建档案表单中移除一个首选城市。 */
      removePreferredCity(city) {
        this.profileForm.preferredCities = this.profileForm.preferredCities.filter(
          (item) => item !== city
        );
      },

      /** 重新读取当前候选人档案。 */
      async refreshCurrentProfile() {
        if (!this.currentProfileId) {
          return;
        }
        const data = await this.requestJson(`/api/profiles/${this.currentProfileId}`);
        this.updateProfileInState(data.profile);
      },

      /** 恢复当前候选人的聊天历史。 */
      async loadChatHistory() {
        if (!this.currentProfileId) {
          this.setWelcomeMessage();
          return;
        }
        const data = await this.requestJson(
          `/api/chat/history?candidate_id=${this.currentProfileId}&session_id=${encodeURIComponent(this.currentSessionId)}`
        );
        this.renderChatHistory(data.messages || []);
      },

      /** 把后端历史记录转换成 Vue 消息列表。 */
      renderChatHistory(messages) {
        if (!messages.length) {
          this.setWelcomeMessage();
          return;
        }
        this.messages = messages.map((message) => ({
          ...message,
          isError: false,
          isStreaming: false,
          renderedHtml: this.renderMarkdown(message.content),
          taskTrace: this.normalizeTaskTrace(message.metadata?.task_trace, { fromHistory: true }),
        }));
        this.reconcileTaskApprovals();
      },

      /** 设置初始欢迎语。 */
      setWelcomeMessage() {
        this.messages = [
          {
            localId: "welcome",
            role: "assistant",
            content: WELCOME_MESSAGE,
            isError: false,
            isStreaming: false,
            renderedHtml: this.renderMarkdown(WELCOME_MESSAGE),
            taskTrace: null,
          },
        ];
      },

      /** 发送一轮聊天，并在 SSE token 到达时更新同一个助手气泡。 */
      async sendMessage() {
        const message = this.messageInput.trim();
        if (!this.currentProfileId) {
          this.appendAssistant("请先在左侧创建或选择候选人档案。", true);
          return;
        }
        if (!message || this.sending) {
          return;
        }

        this.messageInput = "";
        this.appendUser(message);
        const assistantMessage = this.appendAssistant("", false, true);
        const abortController = new AbortController();
        this.chatAbortController = abortController;
        this.sending = true;
        try {
          const data = await this.streamChatReply(
            {
              candidate_id: this.currentProfileId,
              message,
              use_env_llm: DEFAULT_USE_LANGCHAIN_AGENT,
              auto_rag: DEFAULT_AUTO_INCREMENTAL_RAG,
              session_id: this.currentSessionId,
            },
            assistantMessage,
            { signal: abortController.signal }
          );
          this.updateMessage(
            assistantMessage,
            data.display_reply || this.buildChatReply(data),
            false,
            false
          );
          if (data.task_trace) {
            this.setMessageTaskTrace(assistantMessage, data.task_trace, { autoCollapse: true });
          }
          this.captureProjectTasksFromChat(data.tool_outputs || []);
          if (data.profile) {
            this.updateProfileInState(data.profile);
          }
          await this.loadJobs(abortController.signal);
          // Agent 可能在本轮生成了职位定制文件，侧栏必须同步刷新版本列表。
          await this.loadResumeArtifacts(abortController.signal);
          // Agent 也可能刚刚提交或确认 GitHub 项目卡片，保持侧栏与任务事实同步。
          await this.loadProjectCards(abortController.signal);
          await this.matchJobs(true, abortController.signal);
        } catch (error) {
          const wasCancelled = error?.name === "AbortError";
          const messageText = wasCancelled
            ? "已停止生成。"
            : error?.message || "聊天请求失败，请稍后重试。";
          this.failMessageTaskTrace(assistantMessage, messageText, wasCancelled ? "cancelled" : "failed");
          this.updateMessage(assistantMessage, messageText, !wasCancelled, false);
        } finally {
          if (this.chatAbortController === abortController) {
            this.chatAbortController = null;
          }
          this.sending = false;
        }
      },

      /** 删除当前候选人档案；后端会级联清理档案下的会话、资料和文件。 */
      async deleteCurrentProfile() {
        const profile = this.currentProfile;
        if (
          !profile ||
          !window.confirm(
            `确定删除档案“${profile.name}”吗？\n该档案下的对话、项目经历和简历文件也会被删除。`
          )
        ) {
          return;
        }
        const candidateId = this.currentProfileId;
        this.deletingProfileId = candidateId;
        try {
          await this.requestJson(`/api/profiles/${candidateId}`, { method: "DELETE" });
          localStorage.removeItem("currentProfileId");
          localStorage.removeItem(`activeSessionId:${this.auth.account?.id || "legacy"}:${candidateId}`);
          this.currentProfileId = 0;
          this.activeSessionId = "";
          this.sessions = [];
          this.messages = [];
          this.matches = [];
          this.resumeArtifacts = [];
          this.resumeJobSelections = {};
          this.projectCards = [];
          await this.loadProfiles();
        } catch (error) {
          this.appendAssistant(`删除档案失败：${error.message || "未知错误"}`, true);
        } finally {
          this.deletingProfileId = 0;
        }
      },

      /** 主动取消当前模型请求，避免网络异常时只能等待超时。 */
      cancelChat() {
        this.chatAbortController?.abort();
      },

      /**
       * 消费后端 /api/chat/stream 的 SSE 响应。
       *
       * token 事件只负责增量显示，final 事件才包含完整持久化展示文本、
       * 工具摘要和最新候选人档案。
       */
      async streamChatReply(payload, assistantMessage, options = {}) {
        const externalSignal = options?.signal || null;
        const requestedTimeout = Number(options?.timeoutMs);
        const timeoutMs = requestedTimeout > 0 ? requestedTimeout : CHAT_STREAM_TIMEOUT_MS;
        const controller = new AbortController();
        let timeoutReached = false;
        let reader = null;
        let relayAbort = null;
        const timeoutId = window.setTimeout(() => {
          timeoutReached = true;
          controller.abort();
        }, timeoutMs);

        if (externalSignal) {
          relayAbort = () => controller.abort();
          if (externalSignal.aborted) {
            relayAbort();
          } else {
            externalSignal.addEventListener("abort", relayAbort, { once: true });
          }
        }

        try {
          const response = await fetch("/api/chat/stream", {
            method: "POST",
            credentials: "same-origin",
            headers: {
              "Content-Type": "application/json",
              ...(this.csrfHeaders ? this.csrfHeaders("POST") : {}),
            },
            body: JSON.stringify(payload),
            signal: controller.signal,
          });
          if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.detail || `请求失败：${response.status}`);
          }
          if (!response.body) {
            throw new Error("当前浏览器不支持流式响应读取。");
          }

          reader = response.body.getReader();
          const decoder = new TextDecoder("utf-8");
          let buffer = "";
          let streamedText = "";
          let visibleText = "";
          let finalPayload = null;
          let renderFrameId = null;
          const tokenQueue = [];
          const drainResolvers = [];

          // 浏览器可能把多个 SSE token 合并进同一次 read。这里把收到的内容先排队，
          // 再按动画帧刷新到气泡，避免 Vue 在同一轮事件循环里只渲染最后状态。
          const scheduleTokenRender = () => {
            if (renderFrameId !== null) {
              return;
            }
            const schedule = window.requestAnimationFrame
              ? (callback) => window.requestAnimationFrame(callback)
              : (callback) => window.setTimeout(callback, 16);
            renderFrameId = schedule(flushQueuedTokens);
          };

          // 每帧至少刷一个片段；积压较多时自适应加速，兼顾“能看见流动”和“不拖太久”。
          const flushQueuedTokens = () => {
            renderFrameId = null;
            if (!tokenQueue.length) {
              this.resolveStreamDrainWaiters(drainResolvers);
              return;
            }

            const tokensThisFrame = tokenQueue.length > 90 ? Math.ceil(tokenQueue.length / 45) : 1;
            visibleText += tokenQueue.splice(0, tokensThisFrame).join("");
            this.updateMessage(assistantMessage, visibleText || "生成中...", false, true);

            if (tokenQueue.length) {
              scheduleTokenRender();
            } else {
              this.resolveStreamDrainWaiters(drainResolvers);
            }
          };

          // final 事件可能和最后一批 token 在同一个网络块里到达；返回前先等动画队列刷完。
          const waitForTokenQueue = () => {
            if (!tokenQueue.length && renderFrameId === null) {
              return Promise.resolve();
            }
            return new Promise((resolve) => {
              drainResolvers.push(resolve);
            });
          };

          const enqueueTokenContent = (content) => {
            if (!content) {
              return;
            }
            streamedText += content;
            tokenQueue.push(...this.splitStreamDisplayChunks(content));
            scheduleTokenRender();
          };

          const handleStreamEvent = (event) => {
            if (event.event === "token") {
              enqueueTokenContent(event.data.content || "");
            } else if (event.event === "task_started") {
              this.setMessageTaskTrace(assistantMessage, event.data.task_trace, { forceExpanded: true });
            } else if (event.event === "step_started") {
              this.upsertMessageTaskStep(assistantMessage, event.data.step);
            } else if (event.event === "step_completed") {
              this.upsertMessageTaskStep(assistantMessage, event.data.step);
            } else if (event.event === "task_completed") {
              this.setMessageTaskTrace(assistantMessage, event.data.task_trace, { autoCollapse: true });
            } else if (event.event === "task_failed") {
              this.setMessageTaskTrace(assistantMessage, event.data.task_trace, { forceExpanded: true });
            } else if (event.event === "approval_required") {
              this.setMessageTaskTrace(assistantMessage, event.data.task_trace, { forceExpanded: true });
            } else if (event.event === "status" && !assistantMessage.taskTrace && !streamedText && !visibleText) {
              this.updateMessage(
                assistantMessage,
                event.data.content || "正在调用工具...",
                false,
                true
              );
            } else if (event.event === "final") {
              finalPayload = event.data;
            } else if (event.event === "error") {
              throw new Error(event.data.detail || "流式请求失败。");
            }
          };

          while (true) {
            const { value, done } = await reader.read();
            if (done) {
              break;
            }
            buffer += decoder.decode(value, { stream: true });
            const parsed = this.consumeSseBuffer(buffer);
            buffer = parsed.remaining;
            for (const event of parsed.events) {
              handleStreamEvent(event);
            }
          }

          buffer += decoder.decode();
          const parsed = this.consumeSseBuffer(buffer, true);
          for (const event of parsed.events) {
            handleStreamEvent(event);
          }

          if (!finalPayload) {
            throw new Error("流式响应结束时没有收到 final 事件。");
          }
          if (!streamedText) {
            enqueueTokenContent(finalPayload.display_reply || this.buildChatReply(finalPayload));
          }
          await waitForTokenQueue();
          return finalPayload;
        } catch (error) {
          if (timeoutReached) {
            const timeoutError = new Error("模型响应超时，请检查网络后重试，或点击‘停止生成’。");
            timeoutError.name = "ChatStreamTimeoutError";
            throw timeoutError;
          }
          throw error;
        } finally {
          window.clearTimeout(timeoutId);
          if (externalSignal && relayAbort) {
            externalSignal.removeEventListener("abort", relayAbort);
          }
          if (reader) {
            try {
              await reader.cancel();
            } catch (_error) {
              // 读取器已经结束或被取消时无需重复提示用户。
            }
          }
        }
      },

      /** 把较大的网络片段拆成较小显示片段，防止被浏览器合并后整段闪现。 */
      splitStreamDisplayChunks(content) {
        const text = String(content || "");
        if (!text) {
          return [];
        }
        if (Array.from(text).length <= 8) {
          return [text];
        }

        const chars = Array.from(text);
        const chunkSize = chars.length > 600 ? 12 : 6;
        const chunks = [];
        for (let index = 0; index < chars.length; index += chunkSize) {
          chunks.push(chars.slice(index, index + chunkSize).join(""));
        }
        return chunks;
      },

      /** 唤醒等待流式显示队列刷空的 Promise。 */
      resolveStreamDrainWaiters(waiters) {
        const resolvers = waiters.splice(0);
        for (const resolve of resolvers) {
          resolve();
        }
      },

      /** 解析以空行分隔的 SSE 文本块。 */
      consumeSseBuffer(buffer, flush = false) {
        const normalized = buffer.replace(/\r\n/g, "\n");
        const events = [];
        const parts = normalized.split("\n\n");
        const completeParts = flush ? parts : parts.slice(0, -1);
        const remaining = flush ? "" : parts[parts.length - 1];

        for (const part of completeParts) {
          if (!part.trim()) {
            continue;
          }
          const event = this.parseSseEvent(part);
          if (event) {
            events.push(event);
          }
        }
        return { events, remaining };
      },

      /** 解析一个 SSE 事件的名称和 JSON data。 */
      parseSseEvent(block) {
        let eventName = "message";
        const dataLines = [];
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) {
            eventName = line.slice("event:".length).trim();
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice("data:".length).trimStart());
          }
        }
        const rawData = dataLines.join("\n") || "{}";
        try {
          return { event: eventName, data: JSON.parse(rawData) };
        } catch (error) {
          throw new Error("无法解析服务器返回的 SSE 数据。");
        }
      },

      /** 把后端任务元数据规范成仅供展示的响应式结构。 */
      normalizeTaskTrace(trace, options = {}) {
        if (!trace || typeof trace !== "object") {
          return null;
        }
        const status = String(trace.status || "running");
        const approval = trace.approval && typeof trace.approval === "object"
          ? {
              ...trace.approval,
              items: Array.isArray(trace.approval.items) ? trace.approval.items : [],
              status: trace.approval.status || "waiting",
            }
          : null;
        const shouldStayOpen = ["running", "waiting_confirmation", "failed"].includes(status);
        const expanded = options.forceExpanded
          ? true
          : options.fromHistory
            ? shouldStayOpen
            : Boolean(trace.expanded ?? shouldStayOpen);
        return {
          version: Number(trace.version || 1),
          title: String(trace.title || "本次任务"),
          status,
          duration_ms: Number.isFinite(Number(trace.duration_ms)) ? Number(trace.duration_ms) : null,
          steps: (Array.isArray(trace.steps) ? trace.steps : []).map((step, index) => ({
            id: String(step?.id || `step-${index + 1}`),
            name: String(step?.name || "task_step"),
            label: String(step?.label || "执行任务"),
            status: String(step?.status || "running"),
            summary: step?.summary ? String(step.summary) : "",
          })),
          approval,
          expanded,
        };
      },

      /** 用服务端权威任务状态替换一条助手消息上的任务过程。 */
      setMessageTaskTrace(message, trace, options = {}) {
        const normalized = this.normalizeTaskTrace(trace, options);
        if (!normalized) return;
        if (options.autoCollapse) {
          normalized.expanded = ["waiting_confirmation", "failed"].includes(normalized.status);
        }
        message.taskTrace = normalized;
        this.scrollMessages();
      },

      /** 增量更新一个步骤，避免每次 SSE 事件重建整条消息。 */
      upsertMessageTaskStep(message, step) {
        if (!step || typeof step !== "object") return;
        if (!message.taskTrace) {
          message.taskTrace = this.normalizeTaskTrace({ status: "running", steps: [] }, { forceExpanded: true });
        }
        const normalized = this.normalizeTaskTrace({ steps: [step] })?.steps?.[0];
        if (!normalized) return;
        const index = message.taskTrace.steps.findIndex((item) => item.id === normalized.id);
        if (index >= 0) message.taskTrace.steps[index] = normalized;
        else message.taskTrace.steps.push(normalized);
        message.taskTrace.status = "running";
        message.taskTrace.expanded = true;
        this.scrollMessages();
      },

      /** 任务完成后自动折叠，用户仍可通过摘要行重新展开。 */
      toggleTaskTrace(message) {
        if (!message?.taskTrace) return;
        message.taskTrace.expanded = !message.taskTrace.expanded;
      },

      /** 返回任务摘要行的标题和当前状态。 */
      taskTraceTitle(trace) {
        const stateLabel = {
          running: "进行中",
          completed: "已完成",
          waiting_confirmation: "等待确认",
          failed: "失败",
          cancelled: "已取消",
        }[trace?.status] || "已更新";
        return `${trace?.title || "本次任务"} · ${stateLabel}`;
      },

      /** 把后端毫秒耗时转成紧凑秒数。 */
      taskTraceDuration(trace) {
        if (trace?.status === "running") return "";
        const durationMs = Number(trace?.duration_ms);
        if (!Number.isFinite(durationMs)) return "";
        const seconds = durationMs < 1000 ? (durationMs / 1000).toFixed(1) : Math.round(durationMs / 1000);
        return `（用时 ${seconds} 秒）`;
      },

      /** 失败或用户停止时保留展开状态，让原因可见。 */
      failMessageTaskTrace(message, detail, status = "failed") {
        if (!message?.taskTrace) return;
        message.taskTrace.status = status;
        message.taskTrace.expanded = status === "failed";
        const runningStep = [...message.taskTrace.steps].reverse().find((step) => step.status === "running");
        if (runningStep) {
          runningStep.status = status;
          runningStep.summary = detail;
        }
      },

      /** 查看待确认项目的完整侧栏卡片，不创建新的聊天气泡。 */
      viewTaskApproval(message) {
        if (!message?.taskTrace?.approval) return;
        message.taskTrace.expanded = true;
        if (message.taskTrace.approval.kind === "project_card_confirmation") {
          this.openWorkspacePanel("github");
        }
      },

      /** 执行后端已有的项目经历确认接口，再原地完成任务过程。 */
      async confirmTaskApproval(message) {
        const trace = message?.taskTrace;
        const approval = trace?.approval;
        if (!approval || approval.status !== "waiting" || this.confirmingTaskApprovalId) return;
        if (approval.kind !== "project_card_confirmation" || !approval.record_id) return;

        this.confirmingTaskApprovalId = Number(approval.record_id);
        try {
          const data = await this.requestJson(`/api/projects/${encodeURIComponent(approval.record_id)}/confirm`, {
            method: "POST",
            body: JSON.stringify({ confirmed_summary: null, root_request_id: trace.root_request_id || null }),
          });
          const index = this.projectCards.findIndex((item) => Number(item.id) === Number(approval.record_id));
          if (index >= 0) this.projectCards[index] = data.project_card;
          approval.status = "confirmed";
          approval.message = "已确认，这段项目摘要现在可以作为后续简历和匹配的证据。";
          trace.status = "completed";
          trace.expanded = false;
          if (data.task?.task_key) {
            this.backgroundTasks[data.task.task_key] = data.task;
            this.rememberRagTask(data.task.task_key, 0);
            this.pollBackgroundTask(
              data.task.task_key,
              data.project_card?.card?.project_name || "项目",
              this.currentProfileId
            );
          }
        } catch (error) {
          approval.status = "error";
          approval.message = error.message || "确认失败，请稍后重试。";
          trace.status = "failed";
          trace.expanded = true;
        } finally {
          this.confirmingTaskApprovalId = 0;
        }
      },

      /** 暂不确认只关闭本次提示，不把待确认项目提升为事实证据。 */
      cancelTaskApproval(message) {
        const trace = message?.taskTrace;
        if (!trace?.approval || trace.approval.status !== "waiting") return;
        trace.approval.status = "cancelled";
        trace.approval.message = "本次未使用；项目卡片仍保留在待确认列表中。";
        trace.status = "cancelled";
        trace.expanded = false;
      },

      /** 页面刷新后用项目卡片事实校正历史消息里的旧确认提示。 */
      reconcileTaskApprovals() {
        for (const message of this.messages) {
          const trace = message?.taskTrace;
          const approval = trace?.approval;
          if (approval?.kind !== "project_card_confirmation" || !approval.record_id) continue;
          const record = this.projectCards.find((item) => Number(item.id) === Number(approval.record_id));
          if (record?.status === "已确认") {
            approval.status = "confirmed";
            approval.message = "已确认，这段项目摘要现在可以作为后续简历和匹配的证据。";
            trace.status = "completed";
            trace.expanded = false;
          }
        }
      },

      /** 生成 Agent 模式下的后备展示文本。 */
      buildChatReply(payload) {
        if (payload.mode === "langchain_agent") {
          const toolLine = payload.used_tools?.length
            ? `工具：${payload.used_tools.join("、")}`
            : "工具：本轮未调用工具";
          const toolSummary = this.summarizeToolOutputs(payload.tool_outputs || []);
          return [payload.reply, toolLine, toolSummary].filter(Boolean).join("\n\n");
        }

        const result = payload.result || {};
        const savedFields = result.saved_structured_fields?.length
          ? result.saved_structured_fields.join("、")
          : "无结构化字段";
        const ragLine =
          result.rag_update_mode === "incremental"
            ? "RAG：已增量索引本次长文本"
            : "RAG：本次未更新索引";
        return `${payload.reply}\n\n保存字段：${savedFields}\n长文本 ID：${result.saved_long_text_ids?.join("、") || "无"}\n${ragLine}`;
      },

      /** 把工具结果压缩成适合聊天窗口展示的摘要。 */
      summarizeToolOutputs(toolOutputs) {
        const lines = [];
        for (const item of toolOutputs) {
          const data = item.data || {};
          if (data.error) {
            lines.push(`工具错误：${data.error}`);
          }
          if (Array.isArray(data.saved_structured_fields)) {
            lines.push(`保存字段：${data.saved_structured_fields.join("、") || "无结构化字段"}`);
          }
          if (Array.isArray(data.saved_long_text_ids)) {
            lines.push(`长文本 ID：${data.saved_long_text_ids.join("、") || "无"}`);
          }
          if (data.rag_update_mode) {
            lines.push(
              data.rag_update_mode === "incremental"
                ? "RAG：已增量索引本次长文本"
                : `RAG：${data.rag_update_mode}`
            );
          }
          if (data.job?.title) {
            lines.push(`导入职位：${data.job.title}`);
          }
          if (Array.isArray(data.matches) && data.matches.length) {
            lines.push(`匹配结果：共 ${data.matches.length} 个职位，已按推荐顺序返回。`);
          }
          if (data.task?.task_type === "github_project_analysis") {
            lines.push("GitHub 项目分析：任务已排队，完成后会生成待确认项目卡片。");
          }
        }
        return lines.join("\n");
      },

      /** 上传当前选择的 DOCX/PDF，并跟踪后端安排的 OCR 或 RAG 任务。 */
      async uploadResume(event) {
        this.openWorkspacePanel("resume");
        const input = event?.target || this.$refs.resumeFileInput;
        const file = input?.files?.[0];
        if (!file) {
          return;
        }
        if (!this.currentProfileId) {
          this.resumeError = "请先选择候选人档案。";
          input.value = "";
          return;
        }
        if (file.size > 20 * 1024 * 1024) {
          this.resumeError = "简历文件不能超过 20 MB。";
          input.value = "";
          return;
        }

        this.uploadingResume = true;
        this.resumeError = "";
        try {
          const form = new FormData();
          form.append("candidate_id", String(this.currentProfileId));
          form.append("file", file, file.name);
          const data = await this.requestFormJson("/api/resumes/upload", form);
          await this.loadResumeArtifacts();
          const method = this.resumeExtractionLabel(data.artifact.extraction_method);
          if (data.task?.task_key) {
            // 记录任务键，刷新页面后仍能恢复 OCR/RAG 轮询，不把状态只留在内存里。
            this.rememberRagTask(data.task.task_key, data.artifact.id);
            this.backgroundTasks[data.task.task_key] = data.task;
            this.ragTaskByArtifact[data.artifact.id] = data.task.task_key;
            const taskLabel = data.task.task_type === "resume_ocr" ? "扫描 PDF OCR" : "RAG 增量索引";
            this.appendAssistant(
              `已上传简历：**${data.artifact.download_filename}**\n\n解析方式：${method}\n\n${taskLabel}任务已排队（${data.task.progress}%）。`
            );
            this.pollBackgroundTask(data.task.task_key, data.artifact.download_filename, this.currentProfileId);
          } else {
            const indexLine = data.warning || "简历正文已增量同步到当前账号的 RAG。";
            this.appendAssistant(
              `已上传简历：**${data.artifact.download_filename}**\n\n解析方式：${method}\n\n${indexLine}`,
              Boolean(data.warning)
            );
          }
          if (data.warning && data.task?.task_key) {
            this.resumeError = data.warning;
          }
        } catch (error) {
          if (this.showDuplicateNotice(error, "简历已存在")) {
            return;
          }
          this.resumeError = error.message || "简历上传失败。";
          this.appendAssistant(this.resumeError, true);
        } finally {
          this.uploadingResume = false;
          input.value = "";
        }
      },

      /** 返回当前账号和候选人对应的本地任务键存储位置。 */
      ragTaskStorageKey(candidateId = this.currentProfileId) {
        return `pendingRagTasks:${this.auth.account?.id || "legacy"}:${candidateId}`;
      },

      /** 把任务键写入本地索引，供页面刷新后恢复轮询。 */
      rememberRagTask(taskKey, artifactId) {
        if (!taskKey) return;
        const key = this.ragTaskStorageKey();
        let entries = [];
        try {
          entries = JSON.parse(localStorage.getItem(key) || "[]");
        } catch (_error) {
          entries = [];
        }
        const next = entries.filter((entry) => entry?.task_key !== taskKey);
        next.push({ task_key: taskKey, artifact_id: Number(artifactId || 0) });
        localStorage.setItem(key, JSON.stringify(next.slice(-20)));
      },

      /** 任务结束后从本地待恢复列表中移除任务键。 */
      forgetRagTask(taskKey, candidateId = this.currentProfileId) {
        if (!taskKey) return;
        const key = this.ragTaskStorageKey(candidateId);
        let entries = [];
        try {
          entries = JSON.parse(localStorage.getItem(key) || "[]");
        } catch (_error) {
          entries = [];
        }
        const next = entries.filter((entry) => entry?.task_key !== taskKey);
        if (next.length) localStorage.setItem(key, JSON.stringify(next));
        else localStorage.removeItem(key);
      },

      /** 把 OCR 或 RAG 后台任务状态转换成页面上的短标签。 */
      ragTaskStatus(artifact) {
        const taskKey = this.ragTaskByArtifact[artifact?.id];
        const task = taskKey ? this.backgroundTasks[taskKey] : null;
        if (!task) {
          if (artifact?.status === "processing") return "OCR 等待任务恢复";
          if (artifact?.status === "failed") return "OCR 解析失败";
          return "";
        }
        const taskName = task.task_type === "resume_ocr" ? "OCR" : "RAG";
        return {
          queued: `${taskName} 排队中 ${task.progress}%`,
          running: `${taskName}${taskName === "OCR" ? " 识别中" : " 索引中"} ${task.progress}%`,
          succeeded: `${taskName} 已完成`,
          failed: `${taskName} 失败`,
          cancelled: `${taskName} 已取消`,
        }[task.status] || `${taskName} 状态未知`;
      },

      /** 轮询一个后台任务；OCR 完成后自动继续轮询其创建的 RAG 任务。 */
      async pollBackgroundTask(taskKey, artifactName = "简历", candidateId = this.currentProfileId) {
        if (!taskKey || this.ragTaskPollers[taskKey]) return;
        const poll = async () => {
          try {
            const data = await this.requestJson(`/api/tasks/${encodeURIComponent(taskKey)}`);
            const task = data.task || {};
            this.backgroundTasks[taskKey] = task;
            if (["succeeded", "failed", "cancelled"].includes(task.status)) {
              delete this.ragTaskPollers[taskKey];
              this.forgetRagTask(taskKey, candidateId);
              if (task.status === "succeeded" && task.task_type === "resume_ocr") {
                if (this.currentProfileId === Number(candidateId)) {
                  await this.loadResumeArtifacts();
                }
                const ragTaskKey = task.result?.rag_task_key;
                const artifactId = Number(task.result?.artifact_id || 0);
                if (ragTaskKey) {
                  this.rememberRagTask(ragTaskKey, artifactId);
                  this.backgroundTasks[ragTaskKey] = {
                    task_key: ragTaskKey,
                    task_type: "rag_index",
                    status: "queued",
                    progress: 0,
                  };
                  if (artifactId) this.ragTaskByArtifact[artifactId] = ragTaskKey;
                  this.pollBackgroundTask(ragTaskKey, artifactName, candidateId);
                }
              }
              if (this.currentProfileId === Number(candidateId) && !this.ragTaskNotified[taskKey]) {
                this.ragTaskNotified[taskKey] = true;
                if (task.status === "succeeded") {
                  if (task.task_type === "resume_ocr") {
                    const ragLine = task.result?.rag_task_key
                      ? "OCR 正文已保存，RAG 增量索引已自动开始。"
                      : "OCR 正文已保存。";
                    this.appendAssistant(`**${artifactName}** 的扫描 PDF OCR 已完成。${ragLine}`);
                  } else {
                    const stats = task.result?.index_stats || {};
                    this.appendAssistant(
                      `**${artifactName}** 的 RAG 增量索引已完成，共写入 ${stats.chunk_count || 0} 个文本片段。`
                    );
                  }
                } else {
                  const taskLabel = task.task_type === "resume_ocr" ? "扫描 PDF OCR" : "RAG 增量索引";
                  this.appendAssistant(
                    `**${artifactName}** 的${taskLabel}${task.status === "cancelled" ? "已取消" : "失败"}：${task.error_summary || "请稍后重试。"}`,
                    true
                  );
                }
              }
              return;
            }
            this.ragTaskPollers[taskKey] = window.setTimeout(poll, 1200);
          } catch (error) {
            // 临时网络错误不立即丢弃任务；下一次轮询继续从 PostgreSQL 状态恢复。
            this.ragTaskPollers[taskKey] = window.setTimeout(poll, 3000);
            this.resumeError = error.message || "后台任务状态读取失败。";
          }
        };
        await poll();
      },

      /** 页面刷新或切换档案后恢复仍在排队/执行的 OCR 与 RAG 任务。 */
      resumePendingRagTasks() {
        if (!this.currentProfileId) return;
        const key = this.ragTaskStorageKey();
        let entries = [];
        try {
          entries = JSON.parse(localStorage.getItem(key) || "[]");
        } catch (_error) {
          entries = [];
        }
        for (const entry of entries) {
          if (!entry?.task_key) continue;
          if (entry.artifact_id) this.ragTaskByArtifact[entry.artifact_id] = entry.task_key;
          const artifact = this.resumeArtifacts.find((item) => item.id === Number(entry.artifact_id));
          this.pollBackgroundTask(
            entry.task_key,
            artifact?.download_filename || "简历",
            this.currentProfileId
          );
        }
      },

      /** 返回当前账号和候选人对应的 GitHub 项目任务恢复索引。 */
      projectTaskStorageKey(candidateId = this.currentProfileId) {
        return `pendingGitHubProjectTasks:${this.auth.account?.id || "legacy"}:${candidateId}`;
      },

      /** 记录待完成的项目分析任务，页面刷新后仍可继续从 PostgreSQL 轮询。 */
      rememberProjectTask(taskKey) {
        if (!taskKey) return;
        const key = this.projectTaskStorageKey();
        let taskKeys = [];
        try {
          taskKeys = JSON.parse(localStorage.getItem(key) || "[]");
        } catch (_error) {
          taskKeys = [];
        }
        const next = [...new Set([...taskKeys, taskKey])].slice(-20);
        localStorage.setItem(key, JSON.stringify(next));
      },

      /** 任务结束后清理本地恢复索引，权威历史仍保留在 PostgreSQL。 */
      forgetProjectTask(taskKey, candidateId = this.currentProfileId) {
        if (!taskKey) return;
        const key = this.projectTaskStorageKey(candidateId);
        let taskKeys = [];
        try {
          taskKeys = JSON.parse(localStorage.getItem(key) || "[]");
        } catch (_error) {
          taskKeys = [];
        }
        const next = taskKeys.filter((item) => item !== taskKey);
        if (next.length) localStorage.setItem(key, JSON.stringify(next));
        else localStorage.removeItem(key);
      },

      /** 读取当前候选人的待确认与已确认项目经历卡片。 */
      async loadProjectCards(signal = null) {
        if (!this.currentProfileId) {
          this.projectCards = [];
          return;
        }
        try {
          const data = await this.requestJson(
            `/api/projects?candidate_id=${encodeURIComponent(this.currentProfileId)}`,
            signal ? { signal } : {}
          );
          this.projectCards = data.project_cards || [];
          this.reconcileTaskApprovals();
        } catch (error) {
          this.projectCards = [];
          this.githubProjectError = error.message || "项目卡片加载失败。";
        }
      },

      /** 提交当前候选人的公开 GitHub 仓库，默认由 Worker 异步下载与分析。 */
      async submitGitHubProject() {
        this.openWorkspacePanel("github");
        if (!this.currentProfileId) {
          this.appendAssistant("请先创建或选择候选人档案，再分析 GitHub 项目。", true);
          return;
        }
        const repositoryUrl = this.githubProjectUrl.trim();
        if (!repositoryUrl || this.submittingGitHubProject) return;

        this.submittingGitHubProject = true;
        this.githubProjectError = "";
        try {
          const data = await this.requestJson("/api/projects/github", {
            method: "POST",
            body: JSON.stringify({
              candidate_id: this.currentProfileId,
              repository_url: repositoryUrl,
            }),
          });
          this.githubProjectUrl = "";
          if (data.task?.task_key) {
            this.backgroundTasks[data.task.task_key] = data.task;
            this.rememberProjectTask(data.task.task_key);
            this.appendAssistant("GitHub 项目分析任务已排队，完成后会生成待确认项目经历卡片。");
            this.pollGitHubProjectTask(data.task.task_key, this.currentProfileId);
          } else {
            await this.loadProjectCards();
            this.appendAssistant("GitHub 项目分析已完成，已生成待确认项目经历卡片。");
          }
        } catch (error) {
          if (this.showDuplicateNotice(error, "项目已存在")) {
            return;
          }
          this.githubProjectError = error.message || "GitHub 项目分析提交失败。";
          this.appendAssistant(this.githubProjectError, true);
        } finally {
          this.submittingGitHubProject = false;
        }
      },

      /** 轮询一项 GitHub 项目分析任务，完成后刷新项目卡片而非等待聊天刷新。 */
      async pollGitHubProjectTask(taskKey, candidateId = this.currentProfileId) {
        if (!taskKey || this.projectTaskPollers[taskKey]) return;
        const poll = async () => {
          try {
            const data = await this.requestJson(`/api/tasks/${encodeURIComponent(taskKey)}`);
            const task = data.task || {};
            this.backgroundTasks[taskKey] = task;
            if (["succeeded", "failed", "cancelled"].includes(task.status)) {
              delete this.projectTaskPollers[taskKey];
              this.forgetProjectTask(taskKey, candidateId);
              if (this.currentProfileId === Number(candidateId)) {
                await this.loadProjectCards();
                if (!this.projectTaskNotified[taskKey]) {
                  this.projectTaskNotified[taskKey] = true;
                  if (task.status === "succeeded") {
                    const projectName = task.result?.project_name || "GitHub 项目";
                    this.appendAssistant(`**${projectName}** 已分析完成，请在左侧确认你的实际职责。`);
                  } else {
                    this.githubProjectError = task.error_summary || "GitHub 项目分析失败，请稍后重试。";
                    this.appendAssistant(this.githubProjectError, true);
                  }
                }
              }
              return;
            }
            this.projectTaskPollers[taskKey] = window.setTimeout(poll, 1200);
          } catch (_error) {
            // 网络暂时中断时保留任务键，后续页面刷新或下一次轮询仍能恢复。
            this.projectTaskPollers[taskKey] = window.setTimeout(poll, 3000);
          }
        };
        await poll();
      },

      /** 页面刷新或切换档案后恢复未结束的 GitHub 项目分析任务。 */
      resumePendingProjectTasks() {
        if (!this.currentProfileId) return;
        let taskKeys = [];
        try {
          taskKeys = JSON.parse(localStorage.getItem(this.projectTaskStorageKey()) || "[]");
        } catch (_error) {
          taskKeys = [];
        }
        for (const taskKey of taskKeys) {
          if (typeof taskKey === "string" && taskKey) {
            this.pollGitHubProjectTask(taskKey, this.currentProfileId);
          }
        }
      },

      /** 由候选人明确确认项目卡片，才把其摘要作为后续可检索证据。 */
      async confirmProjectCard(record) {
        if (!record || record.status !== "待确认" || this.confirmingProjectCardId) return;
        this.confirmingProjectCardId = record.id;
        this.githubProjectError = "";
        try {
          const data = await this.requestJson(`/api/projects/${encodeURIComponent(record.id)}/confirm`, {
            method: "POST",
            body: JSON.stringify({ confirmed_summary: null }),
          });
          const index = this.projectCards.findIndex((item) => item.id === record.id);
          if (index >= 0) this.projectCards[index] = data.project_card;
          const projectName = data.project_card?.card?.project_name || "项目";
          if (data.task?.task_key) {
            this.backgroundTasks[data.task.task_key] = data.task;
            this.rememberRagTask(data.task.task_key, 0);
            this.pollBackgroundTask(data.task.task_key, projectName, this.currentProfileId);
            this.appendAssistant(`已确认项目经历：**${projectName}**。RAG 增量索引已自动开始。`);
          } else {
            this.appendAssistant(`已确认项目经历：**${projectName}**。`);
          }
        } catch (error) {
          this.githubProjectError = error.message || "确认项目经历失败。";
          this.appendAssistant(this.githubProjectError, true);
        } finally {
          this.confirmingProjectCardId = 0;
        }
      },

      /** 从 Agent 工具输出中接管 GitHub 分析任务，支持“把链接发给 Agent”这一入口。 */
      captureProjectTasksFromChat(toolOutputs) {
        for (const item of toolOutputs || []) {
          if (item?.tool_name !== "analyze_github_project_for_candidate") continue;
          const task = item.data?.task;
          if (!task?.task_key) continue;
          this.backgroundTasks[task.task_key] = task;
          this.rememberProjectTask(task.task_key);
          this.pollGitHubProjectTask(task.task_key, this.currentProfileId);
        }
      },

      /** 恢复当前候选人的原始和职位定制简历文件列表。 */
      async loadResumeArtifacts(signal = null) {
        if (!this.currentProfileId) {
          this.resumeArtifacts = [];
          this.resumeJobSelections = {};
          return;
        }
        try {
          const data = await this.requestJson(
            `/api/resumes?candidate_id=${encodeURIComponent(this.currentProfileId)}`,
            signal ? { signal } : {}
          );
          this.resumeArtifacts = data.artifacts || [];
          const activeSourceIds = new Set(
            this.resumeArtifacts
              .filter((artifact) => artifact.artifact_type === "source")
              .map((artifact) => String(artifact.id))
          );
          const nextSelections = Object.fromEntries(
            Object.entries(this.resumeJobSelections).filter(([artifactId]) =>
              activeSourceIds.has(String(artifactId))
            )
          );
          for (const artifactId of activeSourceIds) {
            if (!(artifactId in nextSelections)) {
              nextSelections[artifactId] = 0;
            }
          }
          this.resumeJobSelections = nextSelections;
          this.resumePendingRagTasks();
        } catch (error) {
          this.resumeArtifacts = [];
          this.resumeError = error.message || "简历列表加载失败。";
        }
      },

      /** 删除当前候选人的一份原始或职位定制简历文件。 */
      async deleteResumeArtifact(artifact) {
        if (
          !artifact ||
          !window.confirm(
            `确定删除“${artifact.download_filename || "这份简历"}”吗？\n删除后文件和对应的下载记录将无法恢复。`
          )
        ) {
          return;
        }

        this.deletingResumeArtifactId = artifact.id;
        this.resumeError = "";
        try {
          await this.requestJson(`/api/resumes/${encodeURIComponent(artifact.id)}`, { method: "DELETE" });
          delete this.resumeJobSelections[artifact.id];
          await this.loadResumeArtifacts();
        } catch (error) {
          this.resumeError = error.message || "简历删除失败。";
          this.appendAssistant(this.resumeError, true);
        } finally {
          this.deletingResumeArtifactId = 0;
        }
      },

      /** 用选定职位改写原始简历，并刷新 DOCX/PDF 下载版本。 */
      async tailorResume(artifact) {
        const jobId = Number(this.resumeJobSelections[artifact.id] || 0);
        if (!jobId) {
          this.resumeError = "请先为这份简历选择目标职位。";
          return;
        }

        this.tailoringArtifactId = artifact.id;
        this.resumeError = "";
        try {
          const data = await this.requestJson(`/api/resumes/${artifact.id}/tailor`, {
            method: "POST",
            body: JSON.stringify({
              job_id: jobId,
              use_rag: true,
            }),
          });
          await this.loadResumeArtifacts();
          const links = (data.artifacts || [])
            .map((item) => `- [下载 ${this.resumeFileExtension(item)}](${item.download_url})`)
            .join("\n");
          const fallbackWarning = data.draft?.draft?.llm_discarded
            ? "\n\n模型改写未通过真实性检查，当前文件使用了保守回退内容。"
            : "";
          this.appendAssistant(
            `已生成 **${this.jobTitle(jobId)}** 的职位定制简历。\n\n${links}${fallbackWarning}`
          );
        } catch (error) {
          this.resumeError = error.message || "职位定制简历生成失败。";
          this.appendAssistant(this.resumeError, true);
        } finally {
          this.tailoringArtifactId = 0;
        }
      },

      /** 将后端解析方式转换为简短、稳定的页面标签。 */
      resumeExtractionLabel(method) {
        return {
          docx: "Word 文本",
          pdf_text: "PDF 文本层",
          pdf_ocr: "扫描 PDF OCR",
          pdf_mixed: "PDF 文本 + OCR",
          pending_ocr: "等待 OCR",
          ocr_failed: "OCR 失败",
          generated: "Agent 定制",
        }[method] || "已解析";
      },

      /** 区分用户上传的源文件和 Agent 生成的职位定制版本。 */
      resumeArtifactKind(artifact) {
        return artifact.artifact_type === "source" ? "原始" : "定制";
      },

      /** 从文件名提取下载按钮使用的格式标签。 */
      resumeFileExtension(artifact) {
        const parts = String(artifact.download_filename || "文件").split(".");
        return parts.length > 1 ? parts.pop().toUpperCase() : "文件";
      },

      /** 格式化文件大小，避免页面显示难读的原始字节数。 */
      formatFileSize(bytes) {
        const value = Number(bytes || 0);
        if (value >= 1024 * 1024) {
          return `${(value / (1024 * 1024)).toFixed(1)} MB`;
        }
        return `${Math.max(1, Math.round(value / 1024))} KB`;
      },

      /** 根据职位 ID 返回当前账号职位池中的标题。 */
      jobTitle(jobId) {
        return this.jobs.find((job) => Number(job.id) === Number(jobId))?.title || "目标职位";
      },

      /** 切换粘贴文本和截图识别模式，保留来源链接以便后续追溯。 */
      setJobImportMode(mode) {
        if (!["text", "screenshot"].includes(mode)) return;
        this.jobImportMode = mode;
        this.jobImportError = "";
      },

      /** 接住原生文件选择，并在浏览器端先阻止明显超限的上传。 */
      onJobScreenshotChange(event) {
        const input = event?.target || this.$refs.jobScreenshotFiles;
        const files = Array.from(input?.files || []);
        if (files.length > 4) {
          this.jobImportError = "一次最多上传 4 张职位截图。";
          this.showJobImportNotice(this.jobImportError, "无法上传职位截图");
          this.jobForm.screenshots = [];
          if (input) input.value = "";
          return;
        }
        const oversized = files.find((file) => file.size > 8 * 1024 * 1024);
        if (oversized) {
          this.jobImportError = `截图 ${oversized.name} 不能超过 8 MB。`;
          this.showJobImportNotice(this.jobImportError, "无法上传职位截图");
          this.jobForm.screenshots = [];
          if (input) input.value = "";
          return;
        }
        this.jobForm.screenshots = files;
        this.jobImportError = "";
      },

      /** 清理已成功导入的文件选择，允许用户再次选择同一张截图。 */
      clearJobScreenshotSelection() {
        this.jobForm.screenshots = [];
        if (this.$refs.jobScreenshotFiles) this.$refs.jobScreenshotFiles.value = "";
      },

      /** 根据当前导入模式提交文本或用户主动上传的截图。 */
      async importJob() {
        if (this.jobImportMode === "screenshot") {
          await this.importJobScreenshots();
          return;
        }
        this.openWorkspacePanel("job-import");
        const rawText = this.jobForm.rawText.trim();
        if (!rawText) {
          this.jobImportError = "请先粘贴职位文本。";
          this.appendAssistant(this.jobImportError, true);
          return;
        }

        this.jobImportError = "";
        this.importingJob = true;
        try {
          const data = await this.requestJson("/api/jobs", {
            method: "POST",
            body: JSON.stringify({
              raw_text: rawText,
              source_url: this.jobForm.sourceUrl.trim() || null,
            }),
          });
          this.appendAssistant(`已导入职位：${data.job.title}。你可以打开“职位匹配结果”查看排序。`);
          this.jobForm.rawText = "";
          await this.loadJobs();
          await this.matchJobs(true);
        } catch (error) {
          if (this.showDuplicateNotice(error, "职位信息已存在")) {
            return;
          }
          this.jobImportError = error.message;
          this.appendAssistant(this.jobImportError, true);
        } finally {
          this.importingJob = false;
        }
      },

      /** 加载已导入职位列表。 */
      async loadJobs(signal = null) {
        this.loadingJobs = true;
        try {
          const data = await this.requestJson("/api/jobs", signal ? { signal } : {});
          this.jobs = data.jobs || [];
        } finally {
          this.loadingJobs = false;
        }
      },

      /** 上传截图给服务端多模态模型识别，再复用职位审核、去重和匹配流程。 */
      async importJobScreenshots() {
        this.openWorkspacePanel("job-import");
        const screenshots = this.jobForm.screenshots || [];
        if (!screenshots.length) {
          this.jobImportError = "请先选择职位截图。";
          this.appendAssistant(this.jobImportError, true);
          return;
        }

        this.jobImportError = "";
        this.importingJob = true;
        try {
          const form = new FormData();
          const sourceUrl = this.jobForm.sourceUrl.trim();
          if (sourceUrl) form.append("source_url", sourceUrl);
          screenshots.forEach((file) => form.append("screenshots", file, file.name));
          const data = await this.requestFormJson("/api/jobs/screenshots", form);
          this.appendAssistant(`已识别并导入职位：${data.job.title}。你可以打开“职位匹配结果”查看排序。`);
          this.clearJobScreenshotSelection();
          await this.loadJobs();
          await this.matchJobs(true);
        } catch (error) {
          if (this.showDuplicateNotice(error, "职位信息已存在")) {
            return;
          }
          this.jobImportError = error.message;
          if (Number(error?.status) === 400) {
            this.showJobImportNotice(this.jobImportError);
          } else {
            this.appendAssistant(this.jobImportError, true);
          }
        } finally {
          this.importingJob = false;
        }
      },

      /** 保存用户对职位技能重要性分类的人工校正。 */
      async saveJobSkillRequirements(job) {
        if (!job || !Array.isArray(job.skill_requirements)) {
          return;
        }
        this.savingJobSkillsId = job.id;
        try {
          const data = await this.requestJson(`/api/jobs/${encodeURIComponent(job.id)}/skill-requirements`, {
            method: "PUT",
            body: JSON.stringify({ requirements: job.skill_requirements }),
          });
          const index = this.jobs.findIndex((item) => Number(item.id) === Number(job.id));
          if (index >= 0 && data.job) {
            this.jobs[index] = data.job;
          }
          await this.matchJobs(true);
          this.appendAssistant(`已保存“${job.title}”的技能重要性分类，并重新计算匹配度。`);
        } catch (error) {
          this.appendAssistant(`技能分类保存失败：${error.message || "未知错误"}`, true);
        } finally {
          this.savingJobSkillsId = 0;
        }
      },

      /** 删除一个已导入职位及其职位定制简历文件。 */
      async deleteJob(job) {
        if (
          !job ||
          !window.confirm(`确定删除职位“${job.title}”吗？\n该职位的定制简历和匹配结果也会被移除。`)
        ) {
          return;
        }
        this.deletingJobId = job.id;
        try {
          await this.requestJson(`/api/jobs/${job.id}`, { method: "DELETE" });
          await this.loadJobs();
          this.matches = this.matches.filter((item) => Number(item.job?.id) !== Number(job.id));
          Object.keys(this.resumeJobSelections).forEach((artifactId) => {
            if (Number(this.resumeJobSelections[artifactId]) === Number(job.id)) {
              this.resumeJobSelections[artifactId] = 0;
            }
          });
          if (this.currentProfileId) {
            await this.matchJobs(true);
          }
        } catch (error) {
          this.appendAssistant(`删除职位失败：${error.message || "未知错误"}`, true);
        } finally {
          this.deletingJobId = 0;
        }
      },

      /** 请求当前候选人的职位匹配结果。 */
      async matchJobs(silent = false, signal = null) {
        if (!silent) {
          this.openWorkspacePanel("matches");
        }
        if (!this.currentProfileId) {
          this.matches = [];
          if (!silent) {
            this.appendAssistant("请先选择候选人档案。", true);
          }
          return;
        }

        this.loadingMatches = true;
        try {
          const data = await this.requestJson(
            `/api/matches/${this.currentProfileId}`,
            signal ? { signal } : {}
          );
          this.matches = data.matches || [];
        } finally {
          this.loadingMatches = false;
        }
      },

      /** 追加用户消息。 */
      appendUser(text) {
        return this.appendMessage("user", text);
      },

      /** 追加助手消息。 */
      appendAssistant(text, isError = false, isStreaming = false) {
        return this.appendMessage("assistant", text, isError, isStreaming);
      },

      /** 向响应式消息列表追加一条消息。 */
      appendMessage(role, text, isError = false, isStreaming = false) {
        const message = {
          localId: `local-${++this.nextLocalMessageId}`,
          role,
          content: text,
          isError,
          isStreaming,
          renderedHtml: isStreaming ? "" : this.renderMarkdown(text),
          taskTrace: null,
        };
        const reactiveIndex = this.messages.push(message) - 1;
        this.scrollMessages();

        // Vue 3 会在通过响应式数组读取元素时返回 Proxy。后续流式 token 必须修改
        // 这个 Proxy；如果继续修改 push 前的原始对象，界面只会在请求结束后才重绘。
        return this.messages[reactiveIndex];
      },

      /** 更新流式助手气泡，不创建重复消息。 */
      updateMessage(message, text, isError = false, isStreaming = message.isStreaming) {
        message.content = text;
        message.isError = isError;
        message.isStreaming = Boolean(isStreaming);
        if (!message.isStreaming) {
          message.renderedHtml = this.renderMarkdown(text);
        }
        this.scrollMessages();
      },

      /** 让聊天窗口自动滚动到最新消息。 */
      scrollMessages() {
        if (this.messageScrollFrameId !== null) {
          return;
        }
        const schedule = window.requestAnimationFrame
          ? (callback) => window.requestAnimationFrame(callback)
          : (callback) => window.setTimeout(callback, 16);
        this.messageScrollFrameId = schedule(() => {
          this.messageScrollFrameId = null;
          nextTick(() => {
            const container = this.$refs.messages;
            if (container) {
              container.scrollTop = container.scrollHeight;
            }
          });
        });
      },

      /** 用接口返回的新档案替换本地响应式列表中的旧对象。 */
      updateProfileInState(profile) {
        const index = this.profiles.findIndex((item) => item.id === profile.id);
        if (index >= 0) {
          this.profiles[index] = profile;
        } else {
          this.profiles.push(profile);
        }
      },

      /** 返回职位匹配卡片的首条解释。 */
      matchReason(item) {
        return (
          item.match.elimination_reasons?.[0] ||
          item.match.reasons?.[0] ||
          "暂无"
        );
      },

      /** 把后端返回的维度分数和动态权重转成紧凑的可读摘要。 */
      matchDimensionRows(match) {
        const labels = {
          city: "城市",
          salary: "薪资",
          skills: "技能",
          direction: "方向",
          experience: "经验",
        };
        const scores = match?.dimension_scores || {};
        const weights = match?.applied_weights || {};
        return Object.keys(scores)
          .filter((key) => Object.prototype.hasOwnProperty.call(weights, key))
          .map((key) => ({
            key,
            label: labels[key] || key,
            score: Math.round(Number(scores[key]) || 0),
            weight: Number(weights[key]).toFixed(1),
          }));
      },

      /** 返回完整的匹配解释，避免页面只显示一条理由而隐藏风险和改写建议。 */
      matchDetailGroups(match) {
        const groups = [
          { key: "elimination", label: "淘汰原因", items: match?.elimination_reasons || [] },
          { key: "deductions", label: "扣分项", items: match?.deductions || [] },
          { key: "risks", label: "风险", items: match?.risks || [] },
          { key: "uncertainty", label: "字段不确定", items: match?.uncertainty_notes || [] },
          { key: "resume", label: "简历建议", items: match?.resume_suggestions || [] },
        ];
        return groups.filter((group) => Array.isArray(group.items) && group.items.length);
      },

      /** 格式化职位卡片的摘要信息。 */
      formatJobMeta(job) {
        const salary = this.formatSalary(job);
        const requirements = [job.experience_label, job.education].filter(Boolean).join(" · ");
        const company = job.company_name || "公司待确认";
        return [job.city || "城市待确认", salary, requirements, company]
          .filter(Boolean)
          .join(" · ");
      },

      /** 格式化职位薪资。 */
      formatSalary(job) {
        if (job.salary_min_k && job.salary_max_k) {
          return `${job.salary_min_k}-${job.salary_max_k}K`;
        }
        if (job.salary_min_k) {
          return `${job.salary_min_k}K 起`;
        }
        return "薪资待确认";
      },

      /** 截断职位描述，避免工作台卡片过长。 */
      trimText(value, maxLength) {
        const text = String(value || "").replace(/\s+/g, " ").trim();
        return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
      },

      /** 轻量 Markdown 渲染器，先转义 HTML 再处理安全标记。 */
      renderMarkdown(text) {
        const source = String(text ?? "").replace(/\r\n/g, "\n");
        if (!source.trim()) {
          return "";
        }

        const lines = source.split("\n");
        const blocks = [];
        let index = 0;

        while (index < lines.length) {
          const line = lines[index];
          if (!line.trim()) {
            index += 1;
            continue;
          }

          const codeFence = line.match(/^\s*```([\w-]+)?\s*$/);
          if (codeFence) {
            const codeLines = [];
            index += 1;
            while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
              codeLines.push(lines[index]);
              index += 1;
            }
            if (index < lines.length) {
              index += 1;
            }
            const languageClass = codeFence[1]
              ? ` class="language-${this.escapeHtml(codeFence[1])}"`
              : "";
            blocks.push(
              `<pre><code${languageClass}>${this.escapeHtml(codeLines.join("\n"))}</code></pre>`
            );
            continue;
          }

          const table = this.renderMarkdownTable(lines, index);
          if (table) {
            blocks.push(table.html);
            index = table.nextIndex;
            continue;
          }

          const heading = line.match(/^\s*(#{1,4})\s+(.+?)\s*$/);
          if (heading) {
            const level = Math.min(heading[1].length + 2, 6);
            blocks.push(`<h${level}>${this.renderInlineMarkdown(heading[2])}</h${level}>`);
            index += 1;
            continue;
          }

          const unorderedItem = line.match(/^\s*[-*+]\s+(.+)$/);
          if (unorderedItem) {
            const items = [];
            while (index < lines.length) {
              const item = lines[index].match(/^\s*[-*+]\s+(.+)$/);
              if (!item) {
                break;
              }
              items.push(`<li>${this.renderInlineMarkdown(item[1])}</li>`);
              index += 1;
            }
            blocks.push(`<ul>${items.join("")}</ul>`);
            continue;
          }

          const orderedItem = line.match(/^\s*\d+[.)]\s+(.+)$/);
          if (orderedItem) {
            const items = [];
            while (index < lines.length) {
              const item = lines[index].match(/^\s*\d+[.)]\s+(.+)$/);
              if (!item) {
                break;
              }
              items.push(`<li>${this.renderInlineMarkdown(item[1])}</li>`);
              index += 1;
            }
            blocks.push(`<ol>${items.join("")}</ol>`);
            continue;
          }

          const quote = line.match(/^\s*>\s?(.+)$/);
          if (quote) {
            const quoteLines = [];
            while (index < lines.length) {
              const quoteLine = lines[index].match(/^\s*>\s?(.+)$/);
              if (!quoteLine) {
                break;
              }
              quoteLines.push(quoteLine[1]);
              index += 1;
            }
            blocks.push(
              `<blockquote>${this.renderInlineMarkdown(quoteLines.join("\n")).replaceAll(
                "\n",
                "<br>"
              )}</blockquote>`
            );
            continue;
          }

          const paragraphLines = [];
          while (
            index < lines.length &&
            lines[index].trim() &&
            !this.isMarkdownBlockStart(lines[index]) &&
            !this.isMarkdownTableStart(lines, index)
          ) {
            paragraphLines.push(lines[index]);
            index += 1;
          }
          blocks.push(
            `<p>${this.renderInlineMarkdown(paragraphLines.join("\n")).replaceAll(
              "\n",
              "<br>"
            )}</p>`
          );
        }

        return blocks.join("");
      },

      /** 判断当前位置是否由“表头行 + 合法分隔行”组成 Markdown 表格。 */
      isMarkdownTableStart(lines, index) {
        if (index + 1 >= lines.length) {
          return false;
        }

        const headerLine = lines[index];
        const separatorLine = lines[index + 1];
        if (
          !this.hasMarkdownTableDelimiter(headerLine) ||
          !this.hasMarkdownTableDelimiter(separatorLine)
        ) {
          return false;
        }

        const headers = this.splitMarkdownTableRow(headerLine);
        const separators = this.splitMarkdownTableRow(separatorLine);
        return (
          headers.length > 0 &&
          headers.length === separators.length &&
          separators.every((cell) => /^:?-{3,}:?$/.test(cell.trim()))
        );
      },

      /**
       * 从指定行开始渲染一张 Markdown 表格。
       *
       * 正文缺列时补空单元格；多出的列不静默丢弃，而是结束当前表格，交给后续块处理。
       */
      renderMarkdownTable(lines, index) {
        if (!this.isMarkdownTableStart(lines, index)) {
          return null;
        }

        const headers = this.splitMarkdownTableRow(lines[index]);
        const separators = this.splitMarkdownTableRow(lines[index + 1]);
        const alignments = separators.map((cell) => this.markdownTableAlignmentClass(cell));
        const rows = [];
        let nextIndex = index + 2;

        while (nextIndex < lines.length && lines[nextIndex].trim()) {
          const rowLine = lines[nextIndex];
          if (!this.hasMarkdownTableDelimiter(rowLine)) {
            break;
          }

          const cells = this.splitMarkdownTableRow(rowLine);
          if (cells.length > headers.length) {
            break;
          }
          while (cells.length < headers.length) {
            cells.push("");
          }
          rows.push(cells);
          nextIndex += 1;
        }

        const headerHtml = headers
          .map((cell, cellIndex) => {
            const className = alignments[cellIndex];
            const classAttribute = className ? ` class="${className}"` : "";
            return `<th scope="col"${classAttribute}>${this.renderInlineMarkdown(cell)}</th>`;
          })
          .join("");
        const bodyHtml = rows
          .map(
            (cells) =>
              `<tr>${cells
                .map((cell, cellIndex) => {
                  const className = alignments[cellIndex];
                  const classAttribute = className ? ` class="${className}"` : "";
                  return `<td${classAttribute}>${this.renderInlineMarkdown(cell)}</td>`;
                })
                .join("")}</tr>`
          )
          .join("");

        return {
          html: `<div class="markdown-table-wrap"><table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`,
          nextIndex,
        };
      },

      /** 按未转义的竖线拆分表格行，并允许在单元格中使用 `\|`。 */
      splitMarkdownTableRow(line) {
        let value = String(line).trim();
        if (value.startsWith("|")) {
          value = value.slice(1);
        }
        if (
          value.endsWith("|") &&
          !this.isEscapedMarkdownCharacter(value, value.length - 1)
        ) {
          value = value.slice(0, -1);
        }

        const cells = [];
        let current = "";
        let inCode = false;
        for (let charIndex = 0; charIndex < value.length; charIndex += 1) {
          const character = value[charIndex];
          const escaped = this.isEscapedMarkdownCharacter(value, charIndex);
          if (character === "`" && !escaped) {
            inCode = !inCode;
            current += character;
          } else if (character === "|" && !inCode && !escaped) {
            cells.push(current.trim());
            current = "";
          } else if (character === "|" && escaped && current.endsWith("\\")) {
            current = `${current.slice(0, -1)}|`;
          } else {
            current += character;
          }
        }
        cells.push(current.trim());
        return cells;
      },

      /** 判断文本中是否含有可以充当表格列边界的未转义竖线。 */
      hasMarkdownTableDelimiter(line) {
        const value = String(line);
        for (let index = 0; index < value.length; index += 1) {
          if (value[index] === "|" && !this.isEscapedMarkdownCharacter(value, index)) {
            return true;
          }
        }
        return false;
      },

      /** 判断指定字符前是否存在奇数个连续反斜杠。 */
      isEscapedMarkdownCharacter(value, index) {
        let slashCount = 0;
        for (let cursor = index - 1; cursor >= 0 && value[cursor] === "\\"; cursor -= 1) {
          slashCount += 1;
        }
        return slashCount % 2 === 1;
      },

      /** 把表格分隔符的冒号映射为固定 CSS 类，避免把模型文本写入 style。 */
      markdownTableAlignmentClass(separator) {
        const value = String(separator).trim();
        const alignLeft = value.startsWith(":");
        const alignRight = value.endsWith(":");
        if (alignLeft && alignRight) {
          return "markdown-align-center";
        }
        if (alignRight) {
          return "markdown-align-right";
        }
        if (alignLeft) {
          return "markdown-align-left";
        }
        return "";
      },

      /** 渲染行内 Markdown，同时保护代码和链接中的特殊字符。 */
      renderInlineMarkdown(value) {
        const tokens = [];
        let rendered = this.escapeHtml(value);

        rendered = rendered.replace(/`([^`\n]+)`/g, (_, code) =>
          this.stashRenderedToken(tokens, `<code>${code}</code>`)
        );
        rendered = rendered.replace(
          /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
          (_, label, href) =>
            this.stashRenderedToken(
              tokens,
              `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
            )
        );
        rendered = rendered.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
        rendered = rendered.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
        rendered = rendered.replace(
          /(^|[^*])\*([^*\n]+)\*(?!\*)/g,
          "$1<em>$2</em>"
        );
        rendered = rendered.replace(
          /(^|[^_])_([^_\n]+)_(?!_)/g,
          "$1<em>$2</em>"
        );

        return rendered.replace(
          /\u0000MD_TOKEN_(\d+)\u0000/g,
          (_, tokenIndex) => tokens[Number(tokenIndex)] || ""
        );
      },

      /** 保存需要暂时跳过 Markdown 二次处理的 HTML 片段。 */
      stashRenderedToken(tokens, html) {
        const token = `\u0000MD_TOKEN_${tokens.length}\u0000`;
        tokens.push(html);
        return token;
      },

      /** 判断一行是否开启新的 Markdown 块。 */
      isMarkdownBlockStart(line) {
        return (
          /^\s*```/.test(line) ||
          /^\s*#{1,4}\s+/.test(line) ||
          /^\s*[-*+]\s+/.test(line) ||
          /^\s*\d+[.)]\s+/.test(line) ||
          /^\s*>\s?/.test(line)
        );
      },

      /** 将逗号分隔输入转换成干净的字符串数组。 */
      splitItems(value) {
        return String(value || "")
          .replaceAll("，", ",")
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean);
      },

      /** 将技能输入解析成“技能 -> 熟练度”对象。 */
      parseSkills(value) {
        const skills = {};
        for (const item of this.splitItems(value)) {
          const separator = item.includes("=") ? "=" : item.includes(":") ? ":" : "";
          if (!separator) {
            skills[item] = "待确认";
            continue;
          }
          const [skill, level] = item.split(separator);
          if (skill.trim()) {
            skills[skill.trim()] = level.trim() || "待确认";
          }
        }
        return skills;
      },

      /** 格式化技能对象。 */
      formatDict(value) {
        return Object.entries(value || {})
          .map(([key, val]) => `${key}=${val}`)
          .join("、");
      },

      /** 对所有 Markdown 输出先做 HTML 转义，防止 v-html 执行脚本。 */
      escapeHtml(value) {
        return String(value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#039;");
      },

      /** 读取 CSRF cookie，用于浏览器同源状态变更请求。 */
      csrfToken() {
        const match = document.cookie
          .split(";")
          .map((item) => item.trim())
          .find((item) => item.startsWith("job_agent_csrf="));
        if (!match) {
          return "";
        }
        return decodeURIComponent(match.slice("job_agent_csrf=".length));
      },

      /** 只给状态变更请求附加 CSRF header。 */
      csrfHeaders(method = "GET") {
        const normalizedMethod = String(method || "GET").toUpperCase();
        if (["GET", "HEAD", "OPTIONS", "TRACE"].includes(normalizedMethod)) {
          return {};
        }
        const token = this.csrfToken();
        return token ? { "X-CSRF-Token": token } : {};
      },

      /** 统一请求 JSON API，并把后端 detail 转成前端异常。 */
      async requestJson(url, options = {}) {
        const method = options.method || "GET";
        const response = await fetch(url, {
          ...options,
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            ...(this.csrfHeaders ? this.csrfHeaders(method) : {}),
            ...(options.headers || {}),
          },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          const error = new Error(data.detail || `请求失败：${response.status}`);
          error.status = response.status;
          throw error;
        }
        return data;
      },

      /** 发送 multipart/form-data；浏览器负责生成带 boundary 的 Content-Type。 */
      async requestFormJson(url, formData) {
        const response = await fetch(url, {
          method: "POST",
          credentials: "same-origin",
          headers: this.csrfHeaders ? this.csrfHeaders("POST") : {},
          body: formData,
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          const error = new Error(data.detail || `请求失败：${response.status}`);
          error.status = response.status;
          throw error;
        }
        return data;
      },
    },
  }).mount("#app");
}
