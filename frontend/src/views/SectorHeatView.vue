<script setup lang="ts">
import zhCn from 'element-plus/es/locale/lang/zh-cn';
import SectorHeatPanel from '../components/SectorHeatPanel.vue';
</script>

<template>
  <el-config-provider :locale="zhCn">
    <main class="sector-page">
      <header class="sector-page-header">
        <div><span class="page-eyebrow">MARKET OBSERVATORY / 市场观察</span><h1>板块热度与轮动</h1><p>发现关注方向，比较成交参与，追踪热度变化。</p></div>
        <a href="/research">返回研究台</a>
      </header>
      <SectorHeatPanel />
      <p class="sector-page-note">分数用于解释已观察到的市场，不是上涨概率。分歧分较低不代表没有消息或其他未覆盖风险。</p>
    </main>
  </el-config-provider>
</template>

<style scoped>
.sector-page { max-width: 1560px; margin: 0 auto; padding: 22px 36px; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }
.page-eyebrow { display: block; color: #78869c; letter-spacing: 1.5px; font-size: 10px; margin-bottom: 8px; }
.sector-page-header { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
h1 { margin: 0; font-size: 28px; color: #172b4d; font-weight: 650; letter-spacing: -.7px; }
.sector-page-header p, .sector-page-note { color: #697b93; font-size: 13px; line-height: 1.7; }
.sector-page-header a { color: #3d66ae; text-decoration: none; white-space: nowrap; }
@media (max-width: 980px) { .sector-page { padding: 16px 12px; } h1 { font-size: 23px; } .sector-page-header { align-items: center; gap: 8px; } .sector-page-header a { font-size: 12px; } .sector-page-header p { font-size: 12px; margin-bottom: 6px; } .page-eyebrow { font-size: 9px; letter-spacing: .6px; } }
</style>
