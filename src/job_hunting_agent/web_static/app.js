// 本地网页前端逻辑。
// 本文件作为页面私有脚本加载；业务事实落在后端 SQLite/long_texts，RAG 只是从 long_texts 同步出的检索索引。

const state = {
  profiles: [],
  currentProfileId: Number(localStorage.getItem("currentProfileId") || 0),
};

const el = (id) => document.getElementById(id);

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  await loadHealth();
  await loadProfiles();
});

function bindEvents() {
  el("refreshProfilesBtn").addEventListener("click", loadProfiles);
  el("createProfileBtn").addEventListener("click", createProfile);
  el("profileSelect").addEventListener("change", async (event) => {
    state.currentProfileId = Number(event.target.value || 0);
    localStorage.setItem("currentProfileId", String(state.currentProfileId));
    await refreshCurrentProfile();
  });
  el("sendBtn").addEventListener("click", sendMessage);
  el("messageInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      sendMessage();
    }
  });
  el("importJobBtn").addEventListener("click", importJob);
  el("matchJobsBtn").addEventListener("click", matchJobs);
}

async function loadHealth() {
  const badge = el("healthBadge");
  try {
    const data = await requestJson("/api/health");
    const llmText = data.llm?.configured ? "LLM 已配置" : "LLM 本地规则";
    const embeddingText = data.embedding?.configured ? "Embedding 真实" : "Embedding 本地";
    badge.textContent = `${llmText} · ${embeddingText}`;
  } catch (error) {
    badge.textContent = "服务异常";
    badge.classList.add("error");
  }
}

async function loadProfiles() {
  const data = await requestJson("/api/profiles");
  state.profiles = data.profiles;
  const select = el("profileSelect");
  select.innerHTML = "";

  if (!state.profiles.length) {
    select.append(new Option("暂无档案，请先创建", ""));
    state.currentProfileId = 0;
    renderProfileSummary(null);
    return;
  }

  for (const profile of state.profiles) {
    select.append(new Option(`#${profile.id} ${profile.name}`, String(profile.id)));
  }

  if (!state.profiles.some((profile) => profile.id === state.currentProfileId)) {
    state.currentProfileId = state.profiles[0].id;
  }
  select.value = String(state.currentProfileId);
  localStorage.setItem("currentProfileId", String(state.currentProfileId));
  await refreshCurrentProfile();
}

async function createProfile() {
  const payload = {
    name: el("profileName").value.trim(),
    status: "待补充",
    education: el("profileEducation").value.trim() || "待补充",
    experience_years: Number(el("profileExperience").value || 0),
    skills: parseSkills(el("profileSkills").value),
    preferred_cities: splitItems(el("profileCities").value),
    salary_floor_k: null,
    expected_salary_k: null,
    target_directions: splitItems(el("profileDirections").value),
    unacceptable: [],
  };
  if (!payload.name) {
    appendAssistant("请先填写候选人姓名。", true);
    return;
  }

  const data = await requestJson("/api/profiles", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.currentProfileId = data.candidate_id;
  localStorage.setItem("currentProfileId", String(state.currentProfileId));
  appendAssistant(`已创建候选人档案：${data.profile.name}。现在可以直接发送资料。`);
  await loadProfiles();
}

async function refreshCurrentProfile() {
  if (!state.currentProfileId) {
    renderProfileSummary(null);
    return;
  }
  const data = await requestJson(`/api/profiles/${state.currentProfileId}`);
  renderProfileSummary(data.profile);
}

function renderProfileSummary(profile) {
  if (!profile) {
    el("profileSummary").textContent = "请选择或创建候选人档案。";
    return;
  }
  el("profileSummary").textContent = [
    `姓名：${profile.name}`,
    `状态：${profile.status}`,
    `学历：${profile.education}`,
    `经验：${profile.experience_years} 年`,
    `技能：${formatDict(profile.skills) || "暂无"}`,
    `城市：${profile.preferred_cities.join("、") || "暂无"}`,
    `方向：${profile.target_directions.join("、") || "暂无"}`,
    `不可接受：${profile.unacceptable.join("、") || "暂无"}`,
  ].join("\n");
}

async function sendMessage() {
  const input = el("messageInput");
  const message = input.value.trim();
  if (!state.currentProfileId) {
    appendAssistant("请先在左侧创建或选择候选人档案。", true);
    return;
  }
  if (!message) {
    return;
  }

  input.value = "";
  appendUser(message);
  setSending(true);
  try {
    const data = await requestJson("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        candidate_id: state.currentProfileId,
        message,
        use_env_llm: el("useLlmToggle").checked,
        auto_rag: el("autoRagToggle").checked,
      }),
    });
    appendAssistant(buildChatReply(data.result));
    renderProfileSummary(data.profile);
  } catch (error) {
    appendAssistant(error.message, true);
  } finally {
    setSending(false);
  }
}

function buildChatReply(result) {
  const savedFields = result.saved_structured_fields.length
    ? result.saved_structured_fields.join("、")
    : "无结构化字段";
  const ragLine =
    result.rag_update_mode === "incremental"
      ? "RAG：已增量索引本次长文本"
      : "RAG：本次未更新索引";
  return `${result.reply}\n\n保存字段：${savedFields}\n长文本 ID：${result.saved_long_text_ids.join("、") || "无"}\n${ragLine}`;
}

async function importJob() {
  const rawText = el("jobText").value.trim();
  if (!rawText) {
    appendAssistant("请先粘贴职位文本。", true);
    return;
  }
  const data = await requestJson("/api/jobs", {
    method: "POST",
    body: JSON.stringify({
      raw_text: rawText,
      source_url: el("jobSourceUrl").value.trim() || null,
    }),
  });
  appendAssistant(`已导入职位：${data.job.title}。你可以点击右侧“匹配当前候选人”。`);
  el("jobText").value = "";
  await matchJobs();
}

async function matchJobs() {
  if (!state.currentProfileId) {
    appendAssistant("请先选择候选人档案。", true);
    return;
  }
  const data = await requestJson(`/api/matches/${state.currentProfileId}`);
  const container = el("matchResults");
  container.innerHTML = "";
  if (!data.matches.length) {
    container.textContent = "还没有导入职位。";
    return;
  }

  for (const item of data.matches) {
    const card = document.createElement("div");
    card.className = "match-card";
    card.innerHTML = `
      <h3>${escapeHtml(item.job.title)}</h3>
      <div class="match-meta">${escapeHtml(item.job.city || "未知城市")} · ${item.match.tier} · ${item.match.score} 分</div>
      <div class="match-meta">${item.match.eliminated ? "已淘汰：" : "理由："} ${escapeHtml((item.match.elimination_reasons[0] || item.match.reasons[0] || "暂无").toString())}</div>
    `;
    container.append(card);
  }
}

function appendUser(text) {
  appendMessage("user", "你", text);
}

function appendAssistant(text, isError = false) {
  appendMessage("assistant", "A", text, isError);
}

function appendMessage(role, avatar, text, isError = false) {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role}`;
  const bubbleClass = isError ? "bubble error" : "bubble";
  wrapper.innerHTML = `
    <div class="avatar">${avatar}</div>
    <div class="${bubbleClass}"></div>
  `;
  wrapper.querySelector(".bubble").textContent = text;
  el("messages").append(wrapper);
  el("messages").scrollTop = el("messages").scrollHeight;
}

function setSending(isSending) {
  el("sendBtn").disabled = isSending;
  el("sendBtn").textContent = isSending ? "保存中" : "发送";
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `请求失败：${response.status}`);
  }
  return data;
}

function splitItems(value) {
  return value
    .replaceAll("，", ",")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseSkills(value) {
  const skills = {};
  for (const item of splitItems(value)) {
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
}

function formatDict(value) {
  return Object.entries(value || {})
    .map(([key, val]) => `${key}=${val}`)
    .join("、");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
