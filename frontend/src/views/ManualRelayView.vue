<script setup lang="ts">
import { inject } from 'vue';
import { UploadFilled } from '@element-plus/icons-vue';
import { dashboardContextKey } from '../dashboard-context';

const dashboard = inject(dashboardContextKey);
if (!dashboard) throw new Error('manual relay view requires its dashboard context');
const { routes, relayTag, relaySource, relayDate, relayTime, relayText, relayFiles, relayXhr, relayProgress, relayState, addFiles, submitRelay } = dashboard;
</script>

<template>
  <el-card shadow="never" header="手动投递">
    <el-form label-position="top">
      <el-row :gutter="14">
        <el-col :md="12" :xs="24"><el-form-item label="来源"><el-select v-model="relayTag" class="full-width"><el-option v-for="route in routes" :key="route.tag" :label="`#${route.tag} · ${route.label}`" :value="route.tag"/></el-select></el-form-item></el-col>
        <el-col :md="12" :xs="24"><el-form-item label="来源备注"><el-input v-model="relaySource"/></el-form-item></el-col>
        <el-col :md="12" :xs="24"><el-form-item label="日期"><el-date-picker v-model="relayDate" value-format="YYYY-MM-DD" type="date" class="full-width"/></el-form-item></el-col>
        <el-col :md="12" :xs="24"><el-form-item label="时间"><el-time-picker v-model="relayTime" value-format="HH:mm" format="HH:mm" class="full-width"/></el-form-item></el-col>
      </el-row>
      <el-form-item label="正文"><el-input v-model="relayText" type="textarea" :rows="8"/></el-form-item>
      <el-form-item label="媒体"><el-upload drag :auto-upload="false" :show-file-list="false" :on-change="(file: { raw?: File }) => file.raw && addFiles([file.raw])"><el-icon class="upload-icon"><UploadFilled /></el-icon><div>选择文件或拖入此处</div></el-upload><el-space wrap class="section-gap"><el-tag v-for="file in relayFiles" :key="file.name + file.size" closable @close="relayFiles = relayFiles.filter((item: File) => item !== file)">{{ file.name }}</el-tag></el-space></el-form-item>
      <el-progress v-if="relayXhr" :percentage="relayProgress"/><el-alert v-if="relayState" :title="relayState" type="info" :closable="false" class="section-gap"/>
      <el-button type="primary" :loading="!!relayXhr" @click="submitRelay">开始投递</el-button><el-button v-if="relayXhr" @click="relayXhr?.abort(); relayXhr = null">取消</el-button>
    </el-form>
  </el-card>
</template>
