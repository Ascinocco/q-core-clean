(() => {
  'use strict';
  const menu = document.getElementById('app-navigation');
  const trigger = document.getElementById('menu-toggle');
  trigger.addEventListener('click', () => {
    menu.showModal();
    trigger.setAttribute('aria-expanded', 'true');
    document.documentElement.classList.add('navigation-open');
  });
  document.getElementById('menu-close').addEventListener('click', () => menu.close());
  menu.addEventListener('click', event => {
    const rect = menu.getBoundingClientRect();
    if (event.target === menu && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) menu.close();
  });
  menu.addEventListener('close', () => {
    trigger.setAttribute('aria-expanded', 'false');
    document.documentElement.classList.remove('navigation-open');
    trigger.focus();
  });
  // Native modal dialogs handle Escape, focus containment and inert background.
  // Apply one chart theme before the page's existing chart code executes.
  if (window.Highcharts) Highcharts.setOptions({
    chart: {backgroundColor: 'transparent', style: {fontFamily: 'system-ui, sans-serif'}},
    title: {style: {color: '#edf1f5'}},
    xAxis: {lineColor: '#394453', tickColor: '#394453', labels: {style: {color: '#aebdcb'}}, title: {style: {color: '#aebdcb'}}},
    yAxis: {gridLineColor: '#2b3542', labels: {style: {color: '#aebdcb'}}, title: {style: {color: '#aebdcb'}}},
    legend: {itemStyle: {color: '#d4dee8'}, itemHoverStyle: {color: '#ffffff'}, itemHiddenStyle: {color: '#8995a3'}},
    tooltip: {backgroundColor: '#1c2734', borderColor: '#526277', style: {color: '#edf1f5'}},
    credits: {enabled: false},
    accessibility: {enabled: true}
  });
})();
