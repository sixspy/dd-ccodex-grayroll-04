import { api } from "../api.js";
const { ref, onMounted } = Vue;
export const CohortsView = {
  setup() {
    const items = ref([]); const members = ref([]); const shadows = ref([]);
    const cohortId = ref(1);
    const memberForm = ref({ subject_key: "u-new-01" });
    const err = ref(""); const msg = ref("");
    async function reload() {
      items.value = (await api("/api/cohorts")).items || [];
      await Promise.all([loadMembers(), loadShadows()]);
    }
    async function loadMembers() {
      members.value = (await api(`/api/cohorts/${cohortId.value}/members`)).items || [];
    }
    async function loadShadows() {
      shadows.value = (await api("/api/cohorts/member-shadows")).items || [];
    }
    // 甲路径：同事务直写正式册，第二互斥挂靠直接拒写
    async function addMember() {
      err.value = ""; msg.value = "";
      try {
        await api(`/api/cohorts/${cohortId.value}/members`, { method: "POST", body: JSON.stringify(memberForm.value) });
        msg.value = "甲路径：已写入正式成员册";
        await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    // 乙路径：冲突挂靠只进影子成员草案，裁决升正前正式册零改
    async function shadowMember() {
      err.value = ""; msg.value = "";
      try {
        const r = await api(`/api/cohorts/${cohortId.value}/members/shadow`, { method: "POST", body: JSON.stringify(memberForm.value) });
        msg.value = r.conflict ? "乙路径：冲突挂靠已进影子草案，正式册零改" : "乙路径：影子草案已登记，待裁决升正";
        await reload();
      } catch (e) { err.value = e.message || String(e); }
    }
    async function promote(s) {
      err.value = ""; msg.value = "";
      try {
        await api(`/api/cohorts/member-shadows/${s.id}/promote`, { method: "POST", body: "{}" });
        msg.value = `裁决升正：${s.subject_key} 已落正式成员册`;
        await reload();
      } catch (e) { err.value = e.message || String(e); await reload(); }
    }
    async function discard(s) {
      err.value = ""; msg.value = "";
      try {
        await api(`/api/cohorts/member-shadows/${s.id}/discard`, { method: "POST", body: "{}" });
        await loadShadows();
      } catch (e) { err.value = e.message || String(e); }
    }
    onMounted(reload);
    return { items, members, shadows, cohortId, memberForm, err, msg, reload, loadMembers, addMember, shadowMember, promote, discard };
  },
  template: `
  <div class="grid">
    <div class="panel span-4">
      <h2>人群</h2>
      <table><thead><tr><th>码</th><th>标题</th><th>人数</th><th>互斥组</th></tr></thead>
      <tbody><tr v-for="it in items" :key="it.id"><td>{{ it.code }}</td><td>{{ it.title }}</td><td>{{ it.member_count }}</td><td>{{ it.mutex_group || '—' }}</td></tr></tbody></table>
      <div class="field" style="margin-top:1rem"><label>查看人群 ID</label><input type="number" v-model.number="cohortId" /></div>
      <button class="btn secondary" @click="loadMembers">加载成员</button>
    </div>
    <div class="panel span-4">
      <h2>成员</h2>
      <div class="field"><label>主体键</label><input v-model="memberForm.subject_key" /></div>
      <button class="btn" @click="addMember">直接加入（甲）</button>
      <button class="btn secondary" @click="shadowMember">影子草案（乙）</button>
      <div class="err" v-if="err">{{ err }}</div>
      <div class="muted" v-if="msg" style="margin-top:0.45rem">{{ msg }}</div>
      <table style="margin-top:0.8rem"><thead><tr><th>ID</th><th>主体键</th></tr></thead>
      <tbody><tr v-for="m in members" :key="m.id"><td>{{ m.id }}</td><td>{{ m.subject_key }}</td></tr></tbody></table>
    </div>
    <div class="panel span-4">
      <h2>影子成员草案</h2>
      <p class="muted">乙路径台账：裁决升正前正式成员零改；互斥复核不过则拒升废弃。</p>
      <table><thead><tr><th>ID</th><th>人群</th><th>主体键</th><th>状态</th><th>操作</th></tr></thead>
      <tbody><tr v-for="s in shadows" :key="s.id">
        <td>{{ s.id }}</td><td>{{ s.cohort_code }}</td><td>{{ s.subject_key }}</td><td>{{ s.status }}</td>
        <td>
          <template v-if="s.status==='open'">
            <button class="btn" @click="promote(s)">升正</button>
            <button class="btn secondary" @click="discard(s)">废弃</button>
          </template>
          <span v-else class="muted">—</span>
        </td>
      </tr></tbody></table>
    </div>
  </div>`
};
