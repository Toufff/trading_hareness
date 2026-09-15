<script setup lang="ts">
import {ref,onMounted,onBeforeUnmount} from 'vue';
import {getJson} from '../api/http';
import EventResearchPanel, {type EventResearch} from './EventResearchPanel.vue';
import {usePolling} from '../composables/usePolling';
defineProps<{symbols?: string[]}>();
const value=ref<EventResearch>();const error=ref('');let active=true;
const polling=usePolling();
async function refresh(){try{const result=await getJson<EventResearch>('/api/research/strategy/events/latest');if(active){value.value=result;error.value='';}}catch{if(active)error.value='消息研究接口读取失败，以下如有内容仅是上次读取，不等于没有重要消息。';}}
onMounted(()=>polling.every(60_000,refresh));
onBeforeUnmount(()=>{active=false;polling.stop();});
</script>
<template><p v-if="error" role="alert">{{error}}</p><EventResearchPanel :value="value" :symbols="symbols" /></template>
