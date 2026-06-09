/**
 * N-gram Automation Tool - Frontend JavaScript
 */

// DOM Elements
const uploadBox = document.getElementById('upload-box');
const fileInput = document.getElementById('file-input');
const browseBtn = document.getElementById('browse-btn');
const fileInfo = document.getElementById('file-info');
const processSection = document.getElementById('process-section');
const processBtn = document.getElementById('process-btn');
const progressSection = document.getElementById('progress-section');
const progressText = document.getElementById('progress-text');
const resultsSection = document.getElementById('results-section');
const errorSection = document.getElementById('error-section');
const errorMessage = document.getElementById('error-message');
const downloadBtn = document.getElementById('download-btn');
const downloadAsinBtn = document.getElementById('download-asin-btn');
const archiveBtn = document.getElementById('archive-btn');
const resetBtn = document.getElementById('reset-btn');
const retryBtn = document.getElementById('retry-btn');

// State
let selectedFile = null;
let outputFilename = null;
let asinFilename = null;
let lastProcessedData = null;
let analyticsData = null;

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initializeEventListeners();
    initializeNavigation();
    loadArchive();
});

function initializeEventListeners() {
    // Browse button click
    browseBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        fileInput.click();
    });

    // Upload box click
    uploadBox.addEventListener('click', () => {
        fileInput.click();
    });

    // File input change
    fileInput.addEventListener('change', handleFileSelect);

    // Drag and drop
    uploadBox.addEventListener('dragover', handleDragOver);
    uploadBox.addEventListener('dragleave', handleDragLeave);
    uploadBox.addEventListener('drop', handleDrop);

    // Process button
    processBtn.addEventListener('click', processFile);

    // Download button
    downloadBtn.addEventListener('click', downloadFile);

    // Download ASIN button
    if (downloadAsinBtn) {
        downloadAsinBtn.addEventListener('click', downloadAsinFile);
    }

    // Archive button
    archiveBtn.addEventListener('click', archiveReport);

    // Reset button
    resetBtn.addEventListener('click', resetForm);

    // Retry button
    retryBtn.addEventListener('click', resetForm);

    // CTA card click - go to documentation
    const ctaCard = document.querySelector('.cta-card');
    if (ctaCard) {
        ctaCard.addEventListener('click', () => {
            navigateToSection('documentation');
        });
    }
}

function initializeNavigation() {
    // Navigation items
    const navItems = document.querySelectorAll('.nav-item[data-section]');
    navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const section = item.getAttribute('data-section');
            navigateToSection(section);
        });
    });
}

function navigateToSection(sectionName) {
    // Update nav items
    const navItems = document.querySelectorAll('.nav-item[data-section]');
    navItems.forEach(item => {
        item.classList.remove('active');
        if (item.getAttribute('data-section') === sectionName) {
            item.classList.add('active');
        }
    });

    // Update breadcrumb
    const breadcrumb = document.getElementById('breadcrumb-current');
    breadcrumb.textContent = sectionName.charAt(0).toUpperCase() + sectionName.slice(1);

    // Hide all sections
    const sections = document.querySelectorAll('.section-content');
    sections.forEach(section => {
        section.style.display = 'none';
    });

    // Show requested section
    const targetSection = document.getElementById(`section-${sectionName}`);
    if (targetSection) {
        targetSection.style.display = 'block';
    }

    // Special handling for analytics section
    if (sectionName === 'analytics') {
        updateAnalyticsSection();
    }

    // Special handling for archive section
    if (sectionName === 'archive') {
        loadArchive();
    }
}

// Make navigateToSection globally accessible
window.navigateToSection = navigateToSection;

function handleDragOver(e) {
    e.preventDefault();
    e.stopPropagation();
    uploadBox.classList.add('dragover');
}

function handleDragLeave(e) {
    e.preventDefault();
    e.stopPropagation();
    uploadBox.classList.remove('dragover');
}

function handleDrop(e) {
    e.preventDefault();
    e.stopPropagation();
    uploadBox.classList.remove('dragover');

    const files = e.dataTransfer.files;
    if (files.length > 0) {
        handleFile(files[0]);
    }
}

function handleFileSelect(e) {
    const files = e.target.files;
    if (files.length > 0) {
        handleFile(files[0]);
    }
}

function handleFile(file) {
    // Validate file type
    const ext = file.name.toLowerCase().split('.').pop();
    if (!['csv', 'xlsx', 'xls'].includes(ext)) {
        showError('Please upload a CSV or Excel file (.csv, .xlsx, .xls).');
        return;
    }

    // Validate file size (50MB max)
    if (file.size > 50 * 1024 * 1024) {
        showError('File size exceeds 50MB limit.');
        return;
    }

    selectedFile = file;
    
    // Update UI
    fileInfo.textContent = `Selected: ${file.name} (${formatFileSize(file.size)})`;
    fileInfo.classList.add('success');
    
    // Show process section
    processSection.style.display = 'block';
    
    // Scroll to process button
    processSection.scrollIntoView({ behavior: 'smooth' });
}

function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

async function processFile() {
    if (!selectedFile) {
        showError('Please select a file first.');
        return;
    }

    // Show progress
    showSection('progress');
    updateProgress('Uploading file...');

    // Prepare form data
    const formData = new FormData();
    formData.append('file', selectedFile);

    try {
        updateProgress('Processing file...');
        
        const response = await fetch('/upload', {
            method: 'POST',
            body: formData
        });

        const result = await response.json();

        if (result.success) {
            outputFilename = result.output_file;
            asinFilename = result.asin_file;
            lastProcessedData = result;
            
            // Store analytics data
            analyticsData = {
                ...result.summary,
                campaigns: result.campaigns,
                campaignDetails: result.campaign_details || {},
                filename: selectedFile.name,
                processedAt: new Date().toISOString()
            };
            
            displayResults(result);
        } else {
            showError(result.error || 'An error occurred while processing the file.');
        }
    } catch (error) {
        console.error('Error:', error);
        showError('Network error. Please check your connection and try again.');
    }
}

function displayResults(result) {
    // Update summary cards
    document.getElementById('total-rows').textContent = result.summary.original_rows.toLocaleString();
    document.getElementById('asins-removed').textContent = result.summary.asins_removed.toLocaleString();
    document.getElementById('campaigns-count').textContent = result.summary.campaigns_processed.toLocaleString();

    // Update metrics
    document.getElementById('total-spend').textContent = formatCurrency(result.summary.total_spend);
    document.getElementById('total-sales').textContent = formatCurrency(result.summary.total_sales);

    // Update campaigns list
    const campaignsList = document.getElementById('campaign-names');
    campaignsList.innerHTML = '';
    result.campaigns.forEach(campaign => {
        const li = document.createElement('li');
        li.textContent = campaign;
        campaignsList.appendChild(li);
    });

    // Show/hide ASIN download button based on whether there's an ASIN file
    if (downloadAsinBtn) {
        if (result.asin_file) {
            downloadAsinBtn.style.display = 'inline-flex';
        } else {
            downloadAsinBtn.style.display = 'none';
        }
    }

    // Show results section
    showSection('results');
}

function formatCurrency(value) {
    return '$' + parseFloat(value).toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });
}

function downloadFile() {
    if (!outputFilename) {
        showError('No file available for download.');
        return;
    }

    // Create download link
    const link = document.createElement('a');
    link.href = `/download/${outputFilename}`;
    link.download = outputFilename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

function downloadAsinFile() {
    if (!asinFilename) {
        showError('No ASIN file available for download.');
        return;
    }

    // Create download link
    const link = document.createElement('a');
    link.href = `/download/${asinFilename}`;
    link.download = asinFilename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

async function archiveReport() {
    if (!outputFilename || !analyticsData) {
        showError('No report available to archive.');
        return;
    }

    try {
        const response = await fetch('/archive', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                filename: outputFilename,
                originalFilename: selectedFile ? selectedFile.name : 'Unknown',
                summary: analyticsData,
                processedAt: new Date().toISOString()
            })
        });

        const result = await response.json();

        if (result.success) {
            // Show success message
            archiveBtn.innerHTML = `
                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="20 6 9 17 4 12"></polyline>
                </svg>
                Archived!
            `;
            archiveBtn.disabled = true;
            archiveBtn.style.opacity = '0.7';
            
            // Reset button after 2 seconds
            setTimeout(() => {
                archiveBtn.innerHTML = `
                    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M21 8v13H3V8"></path>
                        <path d="M1 3h22v5H1z"></path>
                        <path d="M10 12h4"></path>
                    </svg>
                    Archive Report
                `;
                archiveBtn.disabled = false;
                archiveBtn.style.opacity = '1';
            }, 2000);
        } else {
            showError(result.error || 'Failed to archive report.');
        }
    } catch (error) {
        console.error('Error archiving:', error);
        showError('Failed to archive report. Please try again.');
    }
}

async function loadArchive() {
    try {
        const response = await fetch('/archive');
        const result = await response.json();

        const archiveEmpty = document.getElementById('archive-empty');
        const archiveContent = document.getElementById('archive-content');
        const archiveList = document.getElementById('archive-list');

        if (result.success && result.archives && result.archives.length > 0) {
            archiveEmpty.style.display = 'none';
            archiveContent.style.display = 'block';
            
            archiveList.innerHTML = result.archives.map(item => `
                <div class="archive-item" data-id="${item.id}">
                    <div class="archive-item-info">
                        <div class="archive-item-title">${item.originalFilename || 'Search Term Report'}</div>
                        <div class="archive-item-meta">
                            Processed on ${formatDate(item.processedAt)} | 
                            File: ${item.filename}
                        </div>
                    </div>
                    <div class="archive-item-stats">
                        <div class="archive-stat">
                            <div class="archive-stat-value">${item.summary?.campaigns_processed || 0}</div>
                            <div class="archive-stat-label">Campaigns</div>
                        </div>
                        <div class="archive-stat">
                            <div class="archive-stat-value">${formatCurrency(item.summary?.total_spend || 0)}</div>
                            <div class="archive-stat-label">Spend</div>
                        </div>
                        <div class="archive-stat">
                            <div class="archive-stat-value">${formatCurrency(item.summary?.total_sales || 0)}</div>
                            <div class="archive-stat-label">Sales</div>
                        </div>
                    </div>
                    <div class="archive-item-actions">
                        <button class="btn btn-primary btn-sm" onclick="downloadArchive('${item.filename}')">
                            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                                <polyline points="7 10 12 15 17 10"></polyline>
                                <line x1="12" y1="15" x2="12" y2="3"></line>
                            </svg>
                            Download
                        </button>
                        <button class="btn btn-secondary btn-sm" onclick="viewArchiveAnalytics('${item.id}')">
                            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <line x1="18" y1="20" x2="18" y2="10"></line>
                                <line x1="12" y1="20" x2="12" y2="4"></line>
                                <line x1="6" y1="20" x2="6" y2="14"></line>
                            </svg>
                            Analytics
                        </button>
                        <button class="btn btn-danger btn-sm" onclick="deleteArchive('${item.id}')">
                            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                            </svg>
                            Delete
                        </button>
                    </div>
                </div>
            `).join('');
        } else {
            archiveEmpty.style.display = 'block';
            archiveContent.style.display = 'none';
        }
    } catch (error) {
        console.error('Error loading archive:', error);
    }
}

function formatDate(dateString) {
    const date = new Date(dateString);
    return date.toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    });
}

function downloadArchive(filename) {
    const link = document.createElement('a');
    link.href = `/download/${filename}`;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

async function viewArchiveAnalytics(archiveId) {
    try {
        const response = await fetch(`/archive/${archiveId}`);
        const result = await response.json();

        if (result.success && result.archive) {
            analyticsData = result.archive.summary;
            analyticsData.campaigns = result.archive.summary.campaigns || [];
            analyticsData.campaignDetails = result.archive.summary.campaignDetails || {};
            navigateToSection('analytics');
        }
    } catch (error) {
        console.error('Error loading archive analytics:', error);
    }
}

async function deleteArchive(archiveId) {
    if (!confirm('Are you sure you want to delete this archived report?')) {
        return;
    }

    try {
        const response = await fetch(`/archive/${archiveId}`, {
            method: 'DELETE'
        });

        const result = await response.json();

        if (result.success) {
            loadArchive();
        } else {
            alert(result.error || 'Failed to delete archive.');
        }
    } catch (error) {
        console.error('Error deleting archive:', error);
        alert('Failed to delete archive. Please try again.');
    }
}

// Make archive functions globally accessible
window.downloadArchive = downloadArchive;
window.viewArchiveAnalytics = viewArchiveAnalytics;
window.deleteArchive = deleteArchive;

function updateAnalyticsSection() {
    const analyticsEmpty = document.getElementById('analytics-empty');
    const analyticsContent = document.getElementById('analytics-content');

    if (!analyticsData) {
        analyticsEmpty.style.display = 'block';
        analyticsContent.style.display = 'none';
        return;
    }

    analyticsEmpty.style.display = 'none';
    analyticsContent.style.display = 'block';

    // Update overview stats
    document.getElementById('analytics-campaigns').textContent = 
        (analyticsData.campaigns_processed || analyticsData.campaigns?.length || 0).toLocaleString();
    
    // Calculate total N-grams
    let totalMonograms = 0;
    let totalBigrams = 0;
    let totalTrigrams = 0;
    
    if (analyticsData.campaignDetails) {
        Object.values(analyticsData.campaignDetails).forEach(campaign => {
            totalMonograms += campaign.monograms || 0;
            totalBigrams += campaign.bigrams || 0;
            totalTrigrams += campaign.trigrams || 0;
        });
    }
    
    document.getElementById('analytics-monograms').textContent = totalMonograms.toLocaleString();
    document.getElementById('analytics-bigrams').textContent = totalBigrams.toLocaleString();
    document.getElementById('analytics-trigrams').textContent = totalTrigrams.toLocaleString();

    // Update financial summary
    document.getElementById('analytics-spend').textContent = formatCurrency(analyticsData.total_spend || 0);
    document.getElementById('analytics-sales').textContent = formatCurrency(analyticsData.total_sales || 0);
    
    // Calculate ACOS
    const spend = parseFloat(analyticsData.total_spend) || 0;
    const sales = parseFloat(analyticsData.total_sales) || 0;
    const acos = sales > 0 ? ((spend / sales) * 100).toFixed(1) : '0';
    document.getElementById('analytics-acos').textContent = acos + '%';
    
    // Calculate orders (if available)
    document.getElementById('analytics-orders').textContent = 
        (analyticsData.total_orders || 0).toLocaleString();

    // Update campaign breakdown table
    updateCampaignBreakdownTable();

    // Update distribution grid
    updateDistributionGrid();
}

function updateCampaignBreakdownTable() {
    const tbody = document.getElementById('campaign-breakdown-body');
    tbody.innerHTML = '';

    if (!analyticsData.campaignDetails || Object.keys(analyticsData.campaignDetails).length === 0) {
        // If no detailed data, show basic campaign list
        if (analyticsData.campaigns && analyticsData.campaigns.length > 0) {
            analyticsData.campaigns.forEach(campaign => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td>${campaign}</td>
                    <td>-</td>
                    <td>-</td>
                    <td>-</td>
                    <td>-</td>
                    <td>-</td>
                    <td>-</td>
                `;
                tbody.appendChild(row);
            });
        }
        return;
    }

    Object.entries(analyticsData.campaignDetails).forEach(([campaignName, data]) => {
        const row = document.createElement('tr');
        row.innerHTML = `
            <td>${campaignName}</td>
            <td>${(data.monograms || 0).toLocaleString()}</td>
            <td>${(data.bigrams || 0).toLocaleString()}</td>
            <td>${(data.trigrams || 0).toLocaleString()}</td>
            <td>${(data.search_terms || 0).toLocaleString()}</td>
            <td>${formatCurrency(data.spend || 0)}</td>
            <td>${formatCurrency(data.sales || 0)}</td>
        `;
        tbody.appendChild(row);
    });
}

function updateDistributionGrid() {
    const grid = document.getElementById('distribution-grid');
    grid.innerHTML = '';

    if (!analyticsData.campaignDetails || Object.keys(analyticsData.campaignDetails).length === 0) {
        return;
    }

    Object.entries(analyticsData.campaignDetails).forEach(([campaignName, data]) => {
        const total = (data.monograms || 0) + (data.bigrams || 0) + (data.trigrams || 0);
        if (total === 0) return;

        const monoPercent = ((data.monograms || 0) / total * 100).toFixed(1);
        const biPercent = ((data.bigrams || 0) / total * 100).toFixed(1);
        const triPercent = ((data.trigrams || 0) / total * 100).toFixed(1);

        const card = document.createElement('div');
        card.className = 'distribution-card';
        card.innerHTML = `
            <h4 title="${campaignName}">${campaignName}</h4>
            <div class="distribution-bar">
                <div class="bar-segment bar-mono" style="width: ${monoPercent}%"></div>
                <div class="bar-segment bar-bi" style="width: ${biPercent}%"></div>
                <div class="bar-segment bar-tri" style="width: ${triPercent}%"></div>
            </div>
            <div class="distribution-legend">
                <div class="legend-item">
                    <span class="legend-dot green"></span>
                    <span>Mono: ${data.monograms || 0} (${monoPercent}%)</span>
                </div>
                <div class="legend-item">
                    <span class="legend-dot purple"></span>
                    <span>Bi: ${data.bigrams || 0} (${biPercent}%)</span>
                </div>
                <div class="legend-item">
                    <span class="legend-dot orange"></span>
                    <span>Tri: ${data.trigrams || 0} (${triPercent}%)</span>
                </div>
            </div>
        `;
        grid.appendChild(card);
    });
}

function showSection(section) {
    // Hide all sections within dashboard
    const uploadSection = document.querySelector('.upload-section');
    if (uploadSection) uploadSection.style.display = 'none';
    if (processSection) processSection.style.display = 'none';
    if (progressSection) progressSection.style.display = 'none';
    if (resultsSection) resultsSection.style.display = 'none';
    if (errorSection) errorSection.style.display = 'none';

    // Show requested section
    switch (section) {
        case 'upload':
            if (uploadSection) uploadSection.style.display = 'block';
            break;
        case 'process':
            if (uploadSection) uploadSection.style.display = 'block';
            if (processSection) processSection.style.display = 'block';
            break;
        case 'progress':
            if (progressSection) progressSection.style.display = 'block';
            break;
        case 'results':
            if (resultsSection) resultsSection.style.display = 'block';
            break;
        case 'error':
            if (errorSection) errorSection.style.display = 'block';
            break;
    }
}

function updateProgress(message) {
    progressText.textContent = message;
}

function showError(message) {
    errorMessage.textContent = message;
    showSection('error');
}

function resetForm() {
    // Reset state
    selectedFile = null;
    outputFilename = null;
    asinFilename = null;

    // Reset file input
    fileInput.value = '';

    // Reset file info
    fileInfo.textContent = '';
    fileInfo.classList.remove('success');

    // Reset archive button
    archiveBtn.innerHTML = `
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 8v13H3V8"></path>
            <path d="M1 3h22v5H1z"></path>
            <path d="M10 12h4"></path>
        </svg>
        Archive Report
    `;
    archiveBtn.disabled = false;
    archiveBtn.style.opacity = '1';

    // Hide ASIN download button
    if (downloadAsinBtn) {
        downloadAsinBtn.style.display = 'none';
    }

    // Show upload section
    showSection('upload');
}
