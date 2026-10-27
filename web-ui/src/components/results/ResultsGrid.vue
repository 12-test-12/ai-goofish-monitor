<script setup lang="ts">
import type { ResultItem } from '@/types/result.d.ts'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import ResultCard from './ResultCard.vue'

interface Props {
  results: ResultItem[]
  isLoading: boolean
}

const props = defineProps<Props>()
const { t } = useI18n()
const dailyGroups = computed(() => {
  const groups = new Map<string, ResultItem[]>()
  for (const item of props.results) {
    const day = item.爬取时间?.slice(0, 10) || ''
    if (!groups.has(day)) groups.set(day, [])
    groups.get(day)!.push(item)
  }
  return [...groups].map(([day, items]) => ({ day, items }))
})

const emit = defineEmits<{
  (e: 'toggle-block', item: ResultItem): void
}>()
const skeletonItems = Array.from({ length: 8 }, (_, index) => index)
</script>

<template>
  <div :aria-busy="isLoading">
    <div
      v-if="isLoading"
      class="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
      aria-live="polite"
    >
      <div
        v-for="item in skeletonItems"
        :key="item"
        class="app-surface overflow-hidden"
      >
        <div class="aspect-[4/3] animate-pulse bg-slate-200/70"></div>
        <div class="space-y-3 p-4">
          <div class="h-5 w-4/5 animate-pulse rounded bg-slate-200/70"></div>
          <div class="h-7 w-1/3 animate-pulse rounded bg-slate-200/70"></div>
          <div class="rounded-xl border border-slate-100 bg-slate-50/80 p-3">
            <div class="h-4 w-1/2 animate-pulse rounded bg-slate-200/70"></div>
            <div class="mt-3 h-2 w-full animate-pulse rounded bg-slate-200/70"></div>
            <div class="mt-3 h-4 w-full animate-pulse rounded bg-slate-200/70"></div>
            <div class="mt-2 h-4 w-3/4 animate-pulse rounded bg-slate-200/70"></div>
          </div>
        </div>
      </div>
    </div>
    <div v-else-if="results.length === 0" class="app-surface text-center py-12 text-gray-500">
      {{ t('results.grid.empty') }}
    </div>
    <div v-else class="space-y-8">
      <section v-for="group in dailyGroups" :key="group.day" class="space-y-4">
        <h2 class="flex items-center gap-3 text-lg font-semibold text-slate-800">
          {{ group.day || t('results.grid.unknownDate') }}
          <span class="text-sm font-normal text-slate-500">{{ t('results.grid.dayCount', { count: group.items.length }) }}</span>
        </h2>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
          <ResultCard v-for="(item, index) in group.items" :key="`${item.商品信息.商品ID}-${item.任务名称}-${item.爬取时间}-${index}`" :item="item" @toggle-block="emit('toggle-block', $event)" />
        </div>
      </section>
    </div>
  </div>
</template>
