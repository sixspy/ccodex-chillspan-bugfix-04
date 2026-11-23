export const spectrumViewTemplate = `
<section class="panel">
  <h2>温谱导出</h2>
  <p class="hint">仅已封批批次可导出；落地行数须与封批代绑定点数逐点相等，缺行整笔拒收、正式册零行；影子手稿不晋升、不下发。</p>
  <div class="form-row">
    <div><label>已封批批次</label>
      <select v-model.number="spectrumForm.batch_id"><option v-for="b in sealedBatches" :key="b.id" :value="b.id">{{ b.batch_code }} G{{ b.generation }}</option></select>
    </div>
  </div>
  <div class="actions"><button @click="doSpectrum">导出温谱</button></div>
  <table class="data-table">
    <thead><tr><th>导出号</th><th>批次</th><th>绑定点数</th><th>落地行数</th><th>门闸</th><th>校验</th><th>状态</th></tr></thead>
    <tbody>
      <tr v-for="x in spectrumExports" :key="x.id">
        <td>{{ x.export_ref }}</td>
        <td>{{ x.batch_code }}</td>
        <td>{{ x.bound_point_count }}</td>
        <td>{{ x.point_count }}</td>
        <td><span class="badge" :class="x.row_gate_ok ? 'pass' : 'fail'">{{ x.row_gate_ok ? "齐" : "偏航" }}</span></td>
        <td class="mono">{{ x.checksum }}</td>
        <td>{{ x.status }}</td>
      </tr>
    </tbody>
  </table>
</section>
`;