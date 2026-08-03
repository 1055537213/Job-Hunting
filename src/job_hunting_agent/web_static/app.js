/**
 * Job Hunting Agent Vue 3 前端。
 *
 * 这一层只负责页面状态、用户交互和 SSE 展示：
 * - 结构化事实仍由后端 SQLite 管理；
 * - 长文本仍由后端 long_texts/RAG 管理；
 * - 聊天通过 /api/chat/stream 接收 LangChain Agent 的增量输出；
 * - 页面不直接连接 SQLite，也不直接调用模型供应商 API。
 */

if (!window.Vue) {
  document.body.innerHTML =
    "<main class='boot-error'><h1>Vue 3 加载失败</h1><p>请确认本地静态资源完整，然后刷新页面。</p></main>";
} else {
  const { createApp, nextTick } = window.Vue;
  const DEFAULT_USE_LANGCHAIN_AGENT = true;
  const DEFAULT_AUTO_INCREMENTAL_RAG = true;
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

  const WELCOME_MESSAGE =
    "你好，我会默认通过标准 LangChain Agent 来处理你的聊天请求，并自动把新增长文本增量同步到 RAG。\n你可以先在左侧创建档案，然后直接发资料；如果模型、.env 或 embedding 配置有问题，页面会直接显示后端返回的原因。";

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
        authLoading: false,
        authSuccess: false,
        authError: "",
        activeView: "workspace",
        sessions: [],
        activeSessionId: "",
        admin: {
          accounts: [],
          events: [],
          summary: {},
        },
        profiles: [],
        jobs: [],
        matches: [],
        resumeArtifacts: [],
        resumeJobSelections: {},
        messages: [],
        currentProfileId: Number(localStorage.getItem("currentProfileId") || 0),
        messageInput: "",
        cityGroups: buildSortedCityGroups(),
        profileForm: {
          name: "",
          education: "",
          experienceYears: 0,
          skills: "",
          city: "",
          directions: "",
        },
        jobForm: {
          sourceUrl: "",
          rawText: "",
        },
        health: {
          text: "检查中...",
          error: false,
          agentConfigured: false,
        },
        loadingProfiles: false,
        creatingProfile: false,
        loadingJobs: false,
        importingJob: false,
        loadingMatches: false,
        uploadingResume: false,
        tailoringArtifactId: 0,
        resumeError: "",
        sending: false,
        jobImportError: "",
        commandPaletteOpen: false,
        commandQuery: "",
        activeCommandIndex: 0,
        nextLocalMessageId: 0,
      };
    },

    computed: {
      /** 返回当前选中的候选人档案。 */
      currentProfile() {
        return this.profiles.find((profile) => profile.id === this.currentProfileId) || null;
      },

      /** 把结构化档案转换成右侧摘要框中的可读文本。 */
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
          `城市：${profile.preferred_cities.join("、") || "暂无"}`,
          `方向：${profile.target_directions.join("、") || "暂无"}`,
          `不可接受：${profile.unacceptable.join("、") || "暂无"}`,
        ].join("\n");
      },

      /** 当前档案的活动会话；首次聊天时会自动创建默认会话。 */
      currentSessionId() {
        return this.activeSessionId || `account-${this.auth.account?.id || "legacy"}-candidate-${this.currentProfileId}`;
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
            description: "重新读取 SQLite 中已导入且通过审核的职位。",
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
      /** 查询变化后重置高亮项，避免键盘选择停在不存在的结果上。 */
      commandQuery() {
        this.activeCommandIndex = 0;
      },
    },

    mounted() {
      this.checkAuth();
      document.addEventListener("keydown", this.handleGlobalShortcut);
    },

    beforeUnmount() {
      document.removeEventListener("keydown", this.handleGlobalShortcut);
      document.body.classList.remove("cmdk-lock");
    },

    methods: {
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
        this.authError = "";
        this.authSuccess = false;
        this.authForm.password = "";
      },

      /** 提交登录或普通用户注册。 */
      async submitAuth() {
        this.authLoading = true;
        this.authError = "";
        this.authSuccess = false;
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
            return;
          }
          this.auth.authenticated = true;
          this.auth.account = data.account || null;
          this.activeView = "workspace";
          this.authForm.password = "";
          await this.initialize();
          await nextTick();
          document.querySelector("#chatPanel")?.focus?.();
        } catch (error) {
          this.authError = error.message || "认证请求失败。";
        } finally {
          this.authLoading = false;
        }
      },

      /** 注销当前设备；服务端会撤销 Session 并清理 Cookie。 */
      async logout() {
        try {
          await this.requestJson("/api/auth/logout", { method: "POST" });
        } catch (error) {
          this.authError = error.message || "退出失败。";
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
          this.authError = error.message || "退出所有设备失败。";
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

      /** 加载账号列表和 Token 用量流水；普通用户不会调用这些接口。 */
      async loadAdminData() {
        try {
          const [accounts, summary, events] = await Promise.all([
            this.requestJson("/api/admin/accounts"),
            this.requestJson("/api/admin/usage/summary"),
            this.requestJson("/api/admin/usage/events?limit=200"),
          ]);
          this.admin.accounts = accounts.accounts || [];
          this.admin.summary = {
            ...(summary.summary || {}),
            by_account: summary.by_account || [],
          };
          this.admin.events = events.events || [];
        } catch (error) {
          this.appendAssistant(`后台数据加载失败：${error.message || "未知错误"}`, true);
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
        const item = (this.admin.summary.by_account || []).find(
          (entry) => Number(entry.account_id) === Number(accountId)
        );
        return item?.billable_tokens || 0;
      },

      /** 监听全局 Ctrl/Cmd+K 和 Esc，提供类似工作台的快速动作入口。 */
      handleGlobalShortcut(event) {
        const key = event.key.toLowerCase();
        if ((event.ctrlKey || event.metaKey) && key === "k") {
          event.preventDefault();
          if (this.commandPaletteOpen) {
            this.closeCommandPalette();
          } else {
            this.openCommandPalette();
          }
        } else if (event.key === "Escape" && this.commandPaletteOpen) {
          this.closeCommandPalette();
        }
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
        nextTick(() => {
          this.$refs.jobText?.focus();
        });
      },

      /** 打开当前候选人的本地 DOCX/PDF 文件选择器。 */
      triggerResumeUpload() {
        if (!this.currentProfileId) {
          this.appendAssistant("请先创建或选择候选人档案，再上传简历。", true);
          return;
        }
        this.$refs.resumeFileInput?.click();
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
          this.health = {
            text: `${agentText} · ${llmText} · ${embeddingText}`,
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
          this.resumeJobSelections = {};
          return;
        }
        await this.refreshCurrentProfile();
        await this.loadChatSessions();
        await this.loadChatHistory();
        await this.loadResumeArtifacts();
        await this.matchJobs(true);
      },

      /** 读取当前档案的会话索引；会话内容仍由单独 history 接口恢复。 */
      async loadChatSessions() {
        if (!this.currentProfileId) {
          this.sessions = [];
          this.activeSessionId = "";
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
            preferred_cities: this.profileForm.city ? [this.profileForm.city] : [],
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
            city: "",
            directions: "",
          };
        } catch (error) {
          this.appendAssistant(error.message, true);
        } finally {
          this.creatingProfile = false;
        }
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
        }));
      },

      /** 设置初始欢迎语。 */
      setWelcomeMessage() {
        this.messages = [
          {
            localId: "welcome",
            role: "assistant",
            content: WELCOME_MESSAGE,
            isError: false,
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
        const assistantMessage = this.appendAssistant("");
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
            assistantMessage
          );
          this.updateMessage(
            assistantMessage,
            data.display_reply || this.buildChatReply(data)
          );
          if (data.profile) {
            this.updateProfileInState(data.profile);
          }
          await this.loadJobs();
          await this.matchJobs(true);
        } catch (error) {
          this.updateMessage(assistantMessage, error.message, true);
        } finally {
          this.sending = false;
        }
      },

      /**
       * 消费后端 /api/chat/stream 的 SSE 响应。
       *
       * token 事件只负责增量显示，final 事件才包含完整持久化展示文本、
       * 工具摘要和最新候选人档案。
       */
      async streamChatReply(payload, assistantMessage) {
        const response = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.detail || `请求失败：${response.status}`);
        }
        if (!response.body) {
          throw new Error("当前浏览器不支持流式响应读取。");
        }

        const reader = response.body.getReader();
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
          this.updateMessage(assistantMessage, visibleText || "生成中...");

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
          } else if (event.event === "status" && !streamedText && !visibleText) {
            this.updateMessage(assistantMessage, event.data.content || "正在调用工具...");
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
        }
        return lines.join("\n");
      },

      /** 上传当前选择的 DOCX/PDF，并让后端完成解析、保存和增量 RAG。 */
      async uploadResume(event) {
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
          const indexLine = data.warning || "简历正文已增量同步到当前账号的 RAG。";
          this.appendAssistant(
            `已上传简历：**${data.artifact.download_filename}**\n\n解析方式：${method}\n\n${indexLine}`,
            Boolean(data.warning)
          );
        } catch (error) {
          this.resumeError = error.message || "简历上传失败。";
          this.appendAssistant(this.resumeError, true);
        } finally {
          this.uploadingResume = false;
          input.value = "";
        }
      },

      /** 恢复当前候选人的原始和职位定制简历文件列表。 */
      async loadResumeArtifacts() {
        if (!this.currentProfileId) {
          this.resumeArtifacts = [];
          this.resumeJobSelections = {};
          return;
        }
        try {
          const data = await this.requestJson(
            `/api/resumes?candidate_id=${encodeURIComponent(this.currentProfileId)}`
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
        } catch (error) {
          this.resumeArtifacts = [];
          this.resumeError = error.message || "简历列表加载失败。";
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
              allow_proficiency_upgrade: false,
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

      /** 导入职位文本；后端会先验证它是否确实像招聘职位。 */
      async importJob() {
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
          this.appendAssistant(`已导入职位：${data.job.title}。你可以点击右侧“匹配当前候选人”。`);
          this.jobForm.rawText = "";
          await this.loadJobs();
          await this.matchJobs(true);
        } catch (error) {
          this.jobImportError = error.message;
          this.appendAssistant(this.jobImportError, true);
        } finally {
          this.importingJob = false;
        }
      },

      /** 加载已导入职位列表。 */
      async loadJobs() {
        this.loadingJobs = true;
        try {
          const data = await this.requestJson("/api/jobs");
          this.jobs = data.jobs || [];
        } finally {
          this.loadingJobs = false;
        }
      },

      /** 请求当前候选人的职位匹配结果。 */
      async matchJobs(silent = false) {
        if (!this.currentProfileId) {
          this.matches = [];
          if (!silent) {
            this.appendAssistant("请先选择候选人档案。", true);
          }
          return;
        }

        this.loadingMatches = true;
        try {
          const data = await this.requestJson(`/api/matches/${this.currentProfileId}`);
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
      appendAssistant(text, isError = false) {
        return this.appendMessage("assistant", text, isError);
      },

      /** 向响应式消息列表追加一条消息。 */
      appendMessage(role, text, isError = false) {
        const message = {
          localId: `local-${++this.nextLocalMessageId}`,
          role,
          content: text,
          isError,
        };
        const reactiveIndex = this.messages.push(message) - 1;
        this.scrollMessages();

        // Vue 3 会在通过响应式数组读取元素时返回 Proxy。后续流式 token 必须修改
        // 这个 Proxy；如果继续修改 push 前的原始对象，界面只会在请求结束后才重绘。
        return this.messages[reactiveIndex];
      },

      /** 更新流式助手气泡，不创建重复消息。 */
      updateMessage(message, text, isError = false) {
        message.content = text;
        message.isError = isError;
        this.scrollMessages();
      },

      /** 让聊天窗口自动滚动到最新消息。 */
      scrollMessages() {
        nextTick(() => {
          const container = this.$refs.messages;
          if (container) {
            container.scrollTop = container.scrollHeight;
          }
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

      /** 截断职位描述，避免右侧卡片过长。 */
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

      /** 统一请求 JSON API，并把后端 detail 转成前端异常。 */
      async requestJson(url, options = {}) {
        const response = await fetch(url, {
          ...options,
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            ...(options.headers || {}),
          },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || `请求失败：${response.status}`);
        }
        return data;
      },

      /** 发送 multipart/form-data；浏览器负责生成带 boundary 的 Content-Type。 */
      async requestFormJson(url, formData) {
        const response = await fetch(url, {
          method: "POST",
          credentials: "same-origin",
          body: formData,
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || `请求失败：${response.status}`);
        }
        return data;
      },
    },
  }).mount("#app");
}
