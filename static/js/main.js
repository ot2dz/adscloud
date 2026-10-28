
checkAuth();
setInterval(() => { if (isAdmin()) loadJobs(); }, 5000);
setInterval(() => { if (currentUser) loadStats(); }, 10000);
