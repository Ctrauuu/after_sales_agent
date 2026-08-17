<script setup>
import * as echarts from 'echarts'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  option: { type: Object, required: true },
})

const chartElement = ref()
let chart

/**
 * 输入：组件属性中的 ECharts 配置。
 * 输出：当前图表配置对象。
 * 功能：为深度监听提供稳定的数据读取入口。
 */
function readOption() {
  return props.option
}

/**
 * 输入：隐式读取图表容器和 ``option`` 属性。
 * 输出：无；初始化图表实例并注册窗口尺寸监听。
 * 功能：在组件挂载后创建可自适应尺寸的 ECharts 图表。
 */
async function mountChart() {
  await nextTick()
  if (!chartElement.value) return
  chart = echarts.init(chartElement.value)
  chart.setOption(props.option, true)
  window.addEventListener('resize', resizeChart)
}

/**
 * 输入：隐式读取已创建的图表实例。
 * 输出：无；触发 ECharts 自适应容器尺寸。
 * 功能：响应浏览器窗口变化，避免图表变形或溢出。
 */
function resizeChart() {
  chart?.resize()
}

/**
 * 输入：变更后的 ECharts 配置 ``option``。
 * 输出：无；用新配置覆盖现有图表。
 * 功能：同步异步接口数据到已挂载的图表实例。
 */
function updateChart(option) {
  chart?.setOption(option, true)
}

/**
 * 输入：隐式读取已创建的图表实例和窗口监听器。
 * 输出：无；释放图表资源并移除尺寸监听。
 * 功能：在组件卸载时避免事件和 Canvas 实例泄漏。
 */
function unmountChart() {
  window.removeEventListener('resize', resizeChart)
  chart?.dispose()
  chart = undefined
}

watch(readOption, updateChart, { deep: true })
onMounted(mountChart)
onBeforeUnmount(unmountChart)
</script>

<template>
  <div ref="chartElement" class="echart" />
</template>

<style scoped>
.echart {
  width: 100%;
  height: 100%;
  min-height: 280px;
}
</style>
