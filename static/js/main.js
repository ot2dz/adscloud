
checkAuth();
// Background refreshes only while the tab is on screen (a hidden tab used to
// keep hitting the database every few seconds for nobody).
setInterval(() => { if (!document.hidden && isAdmin()) loadJobs(); }, 5000);
setInterval(() => { if (!document.hidden && currentUser) loadStats(); }, 30000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && currentUser) loadStats();
});
