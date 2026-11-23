export const lineageViewTemplate = `
<section class="panel">
  <h2>冷链血缘</h2>
  <div class="form-row">
    <div><label>来源批次ID</label><input type="number" v-model.number="lineageForm.from_batch_id"/></div>
    <div><label>目标批次ID</label><input type="number" v-model.number="lineageForm.to_batch_id"/></div>
    <div><label>类型</label><input v-model="lineageForm.edge_type"/></div>
    <div><label>说明</label><input v-model="lineageForm.detail"/></div>
  </div>
  <div class="actions"><button @click="addLineageEdge">登记血缘边</button></div>
  <table class="data-table">
    <thead><tr><th>自</th><th>至</th><th>类型</th><th>绑定点数</th><th>落地行数</th><th>门闸</th><th>说明</th><th>时间</th></tr></thead>
    <tbody>
      <tr v-for="e in lineageEdges" :key="e.id">
        <td>{{ e.from_code }}</td>
        <td>{{ e.to_code || "—" }}</td>
        <td>{{ e.edge_type }}</td>
        <td>{{ e.from_bound_point_count }}</td>
        <td>{{ e.from_landed_point_count || 0 }}</td>
        <td><span class="badge" :class="e.from_row_gate_ok ? 'pass' : 'frozen'">{{ e.from_row_gate_ok ? "齐" : "未齐" }}</span></td>
        <td>{{ e.detail }}</td>
        <td>{{ e.created_at }}</td>
      </tr>
    </tbody>
  </table>
</section>
`;