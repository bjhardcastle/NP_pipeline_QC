# -*- coding: utf-8 -*-
"""
Created on Sat Feb 22 14:18:35 2020

@author: svc_ccg
"""
import numpy as np
from matplotlib import pyplot as plt
import matplotlib.gridspec as gridspec
import os
import analysis
from numba import njit
import visual_behavior
from get_sessions import glob_file
import ecephys
from probeSync_qc import get_sync_line_data
import probeSync_qc as probeSync
import json
import cv2


def find_spikes_per_trial(spikes, trial_starts, trial_ends):
    tsinds = np.searchsorted(spikes, trial_starts)
    teinds = np.searchsorted(spikes, trial_ends)
    
    return teinds - tsinds

@njit     
def makePSTH_numba(spikes, startTimes, windowDur, binSize=0.001, convolution_kernel=0.05, avg=True):
    spikes = spikes.flatten()
    startTimes = startTimes - convolution_kernel/2
    windowDur = windowDur + convolution_kernel
    bins = np.arange(0,windowDur+binSize,binSize)
    convkernel = np.ones(int(convolution_kernel/binSize))
    counts = np.zeros(bins.size-1)
    for i,start in enumerate(startTimes):
        startInd = np.searchsorted(spikes, start)
        endInd = np.searchsorted(spikes, start+windowDur)
        counts = counts + np.histogram(spikes[startInd:endInd]-start, bins)[0]
    
    counts = counts/startTimes.size
    counts = np.convolve(counts, convkernel)/(binSize*convkernel.size)
    return counts[convkernel.size-1:-convkernel.size], bins[:-convkernel.size-1]

def makePSTH(spikes,startTimes,windowDur,binSize=0.01, avg=True):
    bins = np.arange(0,windowDur+binSize,binSize)
    counts = np.zeros((len(startTimes),bins.size-1))    
    for i,start in enumerate(startTimes):
        counts[i] = np.histogram(spikes[(spikes>=start) & (spikes<=start+windowDur)]-start,bins)[0]
    if avg:
        counts = counts.mean(axis=0)
    counts /= binSize
    return counts


def plot_rf(mapping_pkl_data, spikes, first_frame_offset, frameAppearTimes, resp_latency=0.025, plot=True, returnMat=False):

    
    rfFlashStimDict = mapping_pkl_data
    rfStimParams = rfFlashStimDict['stimuli'][0] 
    rf_pre_blank_frames = int(rfFlashStimDict['pre_blank_sec']*rfFlashStimDict['fps'])
    first_rf_frame = first_frame_offset + rf_pre_blank_frames
    rf_frameTimes = frameAppearTimes[first_rf_frame:]
    rf_trial_start_times = rf_frameTimes[np.array([f[0] for f in np.array(rfStimParams['sweep_frames'])]).astype(np.int)]

    #extract trial stim info (xpos, ypos, ori)
    sweep_table = np.array(rfStimParams['sweep_table'])   #table with rfstim parameters, indexed by sweep order to give stim for each trial
    sweep_order = np.array(rfStimParams['sweep_order'])   #index of stimuli for sweep_table for each trial
    
    trial_xpos = np.array([pos[0] for pos in sweep_table[sweep_order, 0]])
    trial_ypos = np.array([pos[1] for pos in sweep_table[sweep_order, 0]])
    trial_ori = sweep_table[sweep_order, 3]
    
    xpos = np.unique(trial_xpos)
    ypos = np.unique(trial_ypos)
    ori = np.unique(trial_ori)
    
    respInds = tuple([(np.where(ypos==y)[0][0], np.where(xpos==x)[0][0], np.where(ori==o)[0][0]) for (y,x,o) in zip(trial_ypos, trial_xpos, trial_ori)])
    trial_spikes = find_spikes_per_trial(spikes, rf_trial_start_times+resp_latency, rf_trial_start_times+resp_latency+0.2)
    respMat = np.zeros([ypos.size, xpos.size, ori.size])
    for (respInd, tspikes) in zip(respInds, trial_spikes):
        respMat[respInd] += tspikes
    
#    bestOri = np.unravel_index(np.argmax(respMat), respMat.shape)[-1]

    return respMat


def plot_psth_change_flashes(change_times, spikes, preTime = 0.05, postTime = 0.55, sdfSigma=0.005):
    
    sdf, t = makePSTH_numba(spikes,change_times-preTime,preTime+postTime, convolution_kernel=sdfSigma*2)
    
    return sdf, t


def lickTriggeredLFP(lick_times, lfp, lfp_time, agarChRange=None, num_licks=20, windowBefore=0.5, windowAfter=0.5, min_inter_lick_time = 0.5, behavior_duration=3600): 
    
    first_lick_times = lick_times[lick_times>lfp_time[0]+windowBefore]
    first_lick_times = first_lick_times[:np.min([len(first_lick_times), num_licks])]
    
    probeSampleRate = 1./np.median(np.diff(lfp_time))
    samplesBefore = int(round(windowBefore * probeSampleRate))
    samplesAfter = int(round(windowAfter * probeSampleRate))
    
    last_lick_ind = np.where(lfp_time<=first_lick_times[-1])[0][-1]
    lfp = lfp[:last_lick_ind+samplesAfter+1]
    lfp = lfp - np.mean(lfp, axis=0)[None, :]    

    if agarChRange is not None:
        agar = np.median(lfp[:,agarChRange[0]:agarChRange[1]],axis=1)
        lfp = lfp-agar[:,None]
    
    lickTriggeredAv = np.full([first_lick_times.size, samplesBefore+samplesAfter, lfp.shape[1]], np.nan)
    lick_inds = np.searchsorted(lfp_time, first_lick_times)
    for il, li in enumerate(lick_inds):
       lickTriggeredAv[il, :, :] = lfp[li-samplesBefore:li+samplesAfter] 


    m = np.nanmean(lickTriggeredAv, axis=0)*0.195  #convert to uV
    mtime = np.linspace(-windowBefore, windowAfter, m.size)  
    return m, mtime, first_lick_times


def plot_lick_triggered_LFP(lfp_dict, lick_times, FIG_SAVE_DIR, prefix='', 
                            agarChRange=None, num_licks=20, windowBefore=0.5, 
                            windowAfter=1.5, min_inter_lick_time = 0.5, behavior_duration=3600):
    
    for p in lfp_dict:

        plfp = lfp_dict[p]['lfp']
        lta, ltime, first_lick_times = analysis.lickTriggeredLFP(lick_times, plfp, lfp_dict[p]['time'], 
                                               agarChRange=[325, 350], num_licks=20, windowBefore=windowBefore,
                                               windowAfter=windowAfter, min_inter_lick_time=0.5)
        
        fig, axes = plt.subplots(2,1)
        fig.suptitle(p + ' Lick-triggered LFP, ' + str(len(first_lick_times)) + ' rewarded lick bouts')
        axes[0].imshow(lta.T, aspect='auto')
        axes[1].plot(np.mean(lta, axis=1), 'k')
        for a in axes:
            a.set_xticks(np.arange(0, windowBefore+windowAfter, windowBefore)*2500)
            a.set_xticklabels(np.round(np.arange(-windowBefore, windowAfter, windowBefore), decimals=2))
            
        axes[1].set_xlim(axes[0].get_xlim())
        axes[0].tick_params(bottom=False, labelbottom=False)
        axes[1].set_xlabel('Time from lick bout (s)')
        axes[0].set_ylabel('channel')
        axes[1].set_ylabel('Mean across channels')
        
        save_figure(fig, os.path.join(FIG_SAVE_DIR, prefix+'Probe' + p + ' lick-triggered LFP'))


def get_first_lick_times(lick_times, min_inter_lick_time=0.5, rewarded=True):
    
    first_lick_times = lick_times[np.insert(np.diff(lick_times)>=min_inter_lick_time, 0, True)]
    
    return first_lick_times


def get_rewarded_lick_times(lickTimes, frameTimes, trials, min_inter_lick_time=0.5):
    
    trial_start_frames = np.array(trials['startframe'])
    trial_end_frames = np.array(trials['endframe'])
    trial_start_times = frameTimes[trial_start_frames]
    trial_end_times = frameTimes[trial_end_frames]
    
    first_lick_times = lickTimes[np.insert(np.diff(lickTimes)>=min_inter_lick_time, 0, True)]
    first_lick_trials = get_trial_by_time(first_lick_times, trial_start_times, trial_end_times)
    
    hit = np.array(trials['response_type']=='HIT')
    
    hit_lick_times = first_lick_times[np.where(hit[first_lick_trials])[0]]

    return hit_lick_times


def get_trial_by_time(times, trial_start_times, trial_end_times):
    trials = []
    for time in times:
        if trial_start_times[0]<=time<trial_end_times[-1]:
            trial = np.where((trial_start_times<=time)&(trial_end_times>time))[0][0]
        else:
            trial = -1
        trials.append(trial)
    
    return np.array(trials)


def plot_frame_intervals(vsyncs, behavior_frame_count, mapping_frame_count, 
                         behavior_start_frame, mapping_start_frame, 
                         replay_start_frame, save_dir, prefix=''):
    
    fig, ax = plt.subplots()
    fig.suptitle('stim frame intervals')
    ax.plot(np.diff(vsyncs))
    ax.set_ylim([0, 0.2])
    vline_locs = [behavior_start_frame, mapping_start_frame, 
                  replay_start_frame, replay_start_frame+behavior_frame_count]
    for v in vline_locs:
        ax.axvline(v, color='k', linestyle='--')

    
    ax.set_xlabel('frames')
    ax.set_ylabel('interval, s (capped at 0.2)')
    
    ax.text(behavior_start_frame + behavior_frame_count/2, 0.15, 
            'behavior', horizontalalignment='center')
    ax.text(mapping_start_frame+mapping_frame_count/2, 0.15, 
            'rf', horizontalalignment='center')
    ax.text(replay_start_frame+behavior_frame_count/2, 0.15, 'replay', horizontalalignment='center')
    
    save_figure(fig, os.path.join(save_dir, prefix+'stim_frame_intervals.png'))


def plot_vsync_interval_histogram(vf, FIG_SAVE_DIR, prefix=''):
    
    fig, ax = plt.subplots(constrained_layout=True)
    fig.suptitle('Vsync interval histogram')
    
    bins = np.arange(0, 0.1, 0.002)*1000
    ax.hist(np.diff(vf)*1000, bins=bins)
    v = ax.axvline(16.667, color='k', linestyle='--')
    ax.set_ylabel('number of frame intervals')
    ax.set_xlabel('frame interval (ms)')
    ax.legend([v], ['expected interval'])
    
    save_figure(fig, os.path.join(FIG_SAVE_DIR, prefix+'vsync_interval_histogram.png'))


def vsync_report(vf, FIG_SAVE_DIR, prefix=''):
    
    report = {}
    intervals = np.diff(vf)
    report['mean interval'] = intervals.mean()
    report['median interval'] = np.median(intervals)
    report['std of interval'] = intervals.std()
    
    report['num intervals 0.1 <= x < 1'] = int(np.sum((intervals<1)&(intervals>=0.1)))
    report['num intervals >= 1 (expected = 2)'] = int(np.sum(intervals>=1))
    
    save_json(report, os.path.join(FIG_SAVE_DIR, prefix+'vsync_report.json'))
    
    
def plot_population_change_response(probe_dict, behavior_frame_count, mapping_frame_count, 
                                    trials, FRAME_APPEAR_TIMES, FIG_SAVE_DIR, ctx_units_percentile=66, prefix=''):
    
    probe_color_dict = {'A': 'orange',
                        'B': 'r',
                        'C': 'k',
                        'D': 'g',
                        'E': 'b',
                        'F': 'm'}
    change_frames = np.array(trials['change_frame'].dropna()).astype(int)+1
    active_change_times = FRAME_APPEAR_TIMES[change_frames]
    first_passive_frame = behavior_frame_count + mapping_frame_count
    passive_change_times = FRAME_APPEAR_TIMES[first_passive_frame:][change_frames]
    
    lfig, lax = plt.subplots()
    preTime = 0.05
    postTime = 0.55
    for p in probe_dict:
        
        u_df = probe_dict[p]
        good_units = u_df[(u_df['quality']=='good')&(u_df['snr']>1)]
    #    max_chan = good_units['peak_channel'].max()
    #    # take spikes from the top n channels as proxy for cortex
    #    spikes = good_units.loc[good_units['peak_channel']>max_chan-num_channels_to_take_from_top]['times']
        ctx_bottom_chan = np.percentile(good_units['peak_channel'], ctx_units_percentile)
        spikes = good_units.loc[good_units['peak_channel']>ctx_bottom_chan]['times']
        sdfs = [[],[]]
        for s in spikes:
            s = s.flatten()
            if s.size>3600:
                for icts, cts in enumerate([active_change_times, passive_change_times]): 
                    sdf,t = analysis.plot_psth_change_flashes(cts, s, preTime=preTime, postTime=postTime)
                    sdfs[icts].append(sdf)
     
        # plot population change response
        fig, ax = plt.subplots()
        title = p + ' population change response'
        fig.suptitle(title)
        ax.plot(t, np.mean(sdfs[0], axis=0), 'k')
        ax.plot(t, np.mean(sdfs[1], axis=0), 'g')
        ax.legend(['active', 'passive'])
        ax.axvline(preTime, c='k')
        ax.axvline(preTime+0.25, c='k')
        ax.set_xticks(np.arange(0, preTime+postTime, 0.05))
        ax.set_xticklabels(np.round(np.arange(-preTime, postTime, 0.05), decimals=2))
        ax.set_xlabel('Time from change (s)')
        ax.set_ylabel('Mean population response')
        save_figure(fig, os.path.join(FIG_SAVE_DIR, prefix + title + '.png'))
        #fig.savefig(os.path.join(FIG_SAVE_DIR, title + '.png'))
        
        mean_active = np.mean(sdfs[0], axis=0)
        mean_active_baseline = mean_active[:int(preTime*1000)].mean()
        baseline_subtracted = mean_active - mean_active_baseline
        lax.plot(t, baseline_subtracted/baseline_subtracted.max(), c=probe_color_dict[p])
        
    lax.legend(probe_dict.keys())
    lax.set_xlim([preTime, preTime+0.1])
    lax.set_xticks(np.arange(preTime, preTime+0.1, 0.02))
    lax.set_xticklabels(np.arange(0, 0.1, 0.02))
    lax.set_xlabel('Time from change (s)')
    lax.set_ylabel('Normalized response')
    save_figure(lfig, os.path.join(FIG_SAVE_DIR, prefix+'pop_change_response_latency_comparison.png'))
    #lfig.savefig(os.path.join(FIG_SAVE_DIR, 'pop_change_response_latency_comparison.png'))
    
    
    
def plot_running_wheel(behavior_data, mapping_data, replay_data, FIG_SAVE_DIR, prefix=''):   
    
    ### Plot Running Wheel Data ###    
    rfig, rax = plt.subplots()
    rfig.set_size_inches(12, 4)
    rfig.suptitle('Running')
    time_offset = 0
    colors = ['k', 'g', 'r']
    for ri, rpkl in enumerate([behavior_data, mapping_data, replay_data]):
        key = 'behavior' if 'behavior' in rpkl['items'] else 'foraging'
        intervals = rpkl['items']['behavior']['intervalsms'] if 'intervalsms' not in rpkl else rpkl['intervalsms']
        time = np.insert(np.cumsum(intervals), 0, 0)/1000.
        
        dx,vsig,vin = [rpkl['items'][key]['encoders'][0][rkey] for rkey in ('dx','vsig','vin')]
        
        run_speed = visual_behavior.analyze.compute_running_speed(dx[:len(time)],time,vsig[:len(time)],vin[:len(time)])
        rax.plot(time+time_offset, run_speed, colors[ri])
        time_offset = time_offset + time[-1]
        
    rax.set_xlabel('Time (s)')
    rax.set_ylabel('Run Speed (cm/s)')
    rax.legend(['behavior', 'rf map', 'passive'])
    save_figure(rfig, os.path.join(FIG_SAVE_DIR, prefix+'run_speed.png'))
    #rfig.savefig(os.path.join(FIG_SAVE_DIR, 'run_speed.png'))


def plot_unit_quality_hist(probe_dict, FIG_SAVE_DIR, prefix=''):
    
    fig, ax = plt.subplots()
    legend_artist = []
    legend_label = []
    for ip, probe in enumerate(probe_dict):
        p = probe_dict[probe]
        
        labels = np.sort(np.unique(p['quality']))
        count = []
        colors = ['g', '0.4']
        bottom = 0
        
        for il, l in enumerate(labels):
            num_units = np.sum(p['quality']==l)
            count.append(num_units)
            
            if l not in legend_label:
                b = ax.bar(ip, num_units, bottom=bottom, color=colors[il])
                legend_artist.append(b)
                legend_label.append(l)
            else:
                ax.bar(ip, num_units, bottom=bottom, color=colors[il])
            bottom = np.cumsum(count)
            
        
    ax.set_xticks(np.arange(len(probe_dict)))
    ax.set_xticklabels([p for p in probe_dict])

    ax.legend(legend_artist, legend_label)
    ax.set_xlabel('Probe')
    ax.set_ylabel('Unit count')
    
    save_figure(fig, os.path.join(FIG_SAVE_DIR, prefix+'unit_quality_hist.png'))
    #fig.savefig(os.path.join(FIG_SAVE_DIR, 'unit_quality_hist.png'))


def plot_unit_distribution_along_probe(probe_dict, FIG_SAVE_DIR, prefix=''):
    
    for ip, probe in enumerate(probe_dict):
        p = probe_dict[probe]
        good_units = p[p['quality']=='good']
        noise_units = p[p['quality']=='noise']
        
        fig, ax = plt.subplots()
        fig.suptitle('Probe {} unit distribution'.format(probe))
        
        bins = np.arange(0, good_units['peakChan'].max(), 20)
        goodhist = ax.hist(good_units['peakChan'], bins=bins, color='g')
        noisehist = ax.hist(noise_units['peakChan'], bins=bins, color='k', alpha=0.8)
    
        ax.set_xlabel('peak channel')
        ax.set_ylabel('unit count')
        
        ax.legend([goodhist[2][0], noisehist[2][0]], ['good', 'noise'])
        
        save_figure(fig, os.path.join(FIG_SAVE_DIR, prefix+'Probe_{}_unit_distribution.png'.format(probe)))
        #fig.savefig(os.path.join(FIG_SAVE_DIR, 'Probe_{}_unit_distribution.png'.format(probe)))
    
  
def plot_all_spike_hist(probe_dict, FIG_SAVE_DIR, prefix=''):
    
    flatten = lambda l: [item[0] for sublist in l for item in sublist]

    for p in probe_dict:
        u_df = probe_dict[p]
        good_units = u_df[(u_df['quality']=='good')&(u_df['snr']>1)]
        
        spikes = flatten(good_units['times'].to_list())
        binwidth = 1
        bins = np.arange(0, np.max(spikes), binwidth)
        hist, bin_e = np.histogram(spikes, bins)
        
        fig, ax = plt.subplots()
        fig.suptitle('spike histogram (good units), Probe ' + p)
        ax.plot(bin_e[1:-1], hist[1:])
        ax.set_xlabel('Time (s)')
        ax.set_ylabel('Spike Count per ' + str(binwidth) + ' second bin')
        save_figure(fig, os.path.join(FIG_SAVE_DIR, prefix+'Probe' + p + ' spike histogram'))
    
#    for ip, probe in enumerate(probe_dirs):
#        p_name = probe.split('_')[-2][-1]
#        base = os.path.join(os.path.join(probe, 'continuous'), 'Neuropix-PXI-100.0')
#        times_file = glob_file(base, 'spike_times.npy')
#        if times_file is not None:
#            times = np.load(times_file)
#            times = times/30000.
#            
#            fig, ax = plt.subplots()
#            bins = np.arange(0, times.max(), 1)
#            hist, b = np.histogram(times, bins=bins)
#            ax.plot(b[:-1], hist, 'k')
#            
#            fig.suptitle('Probe {} all spike time histogram'.format(p_name))
#            save_figure(fig, os.path.join(FIG_SAVE_DIR, prefix+'Probe_{}_all_spike_time_hist.png'.format(p_name)))


def plot_barcode_interval_hist(probe_dirs, syncDataset, FIG_SAVE_DIR, prefix=''):
    
    for ip, probe in enumerate(probe_dirs):
        p_name = probe.split('_')[-2][-1]        
        base = os.path.join(probe, r'events\\Neuropix-PXI-100.0\\TTL_1')
        
        channel_states_file = glob_file(base, 'channel_states.npy')
        event_times_file = glob_file(base, 'event_timestamps.npy')
        
        if channel_states_file and event_times_file:
            
            #get barcode intervals from probe events file                        
            channel_states = np.load(channel_states_file)
            event_times = np.load(event_times_file)
            
            beRising = event_times[channel_states>0]/30000.
            beFalling = event_times[channel_states<0]/30000.
            be_t, be = ecephys.extract_barcodes_from_times(beRising, beFalling)
            
            barcode_intervals = np.diff(be_t)
            
            #get intervals from sync file for comparison
            bRising, bFalling = get_sync_line_data(syncDataset, channel=0)
            bs_t, bs = ecephys.extract_barcodes_from_times(bRising, bFalling)
            
            sync_barcode_intervals = np.diff(bs_t)
            
            fig, ax = plt.subplots(1,2)
            fig.set_size_inches([8, 4])
            bins = np.arange(np.min([barcode_intervals.min(), sync_barcode_intervals.min()])-1, 
                             np.max([barcode_intervals.max(), sync_barcode_intervals.max()])+1)

            ax[0].hist(barcode_intervals, bins)
            ax[1].hist(sync_barcode_intervals, bins)
            ax[0].axhline(len(be_t)-1)
            ax[1].axhline(len(bs_t)-1)
            
            ax[0].set_ylim([0.1, len(be_t)])
            ax[1].set_ylim([0.1, len(bs_t)])
            
            [a.set_yscale('log') for a in ax]
            
            
            ax[0].set_title('Probe {} ephys barcode intervals'.format(p_name))
            ax[1].set_title('Probe {} sync barcode intervals'.format(p_name))
            
            save_figure(fig, os.path.join(FIG_SAVE_DIR, prefix+'Probe_{}_barcode_interval_hist.png'.format(p_name)))

    
def probe_sync_report(probe_dirs, syncDataset, FIG_SAVE_DIR, prefix=''):
 
    bs_t, bs = probeSync.get_sync_barcodes(syncDataset)
 
    alignment_dict = {}    
    for ip, probe in enumerate(probe_dirs):
        
        p_name = probe.split('_')[-2][-1]     
        alignment_dict[p_name] = {}
        
        be_t, be = probeSync.get_ephys_barcodes(probe)
        shift, p_sampleRate, m_endpoints = ecephys.get_probe_time_offset(bs_t, bs, be_t, be, 0, 30000)
        
        alignment_dict[p_name]['shift'] = np.float(shift)
        alignment_dict[p_name]['sample_rate'] = np.float(p_sampleRate)

    save_file = os.path.join(FIG_SAVE_DIR, prefix+'probe_sync_registration.json')
    save_json(alignment_dict, save_file)



def lost_camera_frame_report(paths, FIG_SAVE_DIR, prefix='', cam_report_keys=None):
    
    if cam_report_keys is None:
        cam_report_keys = [('RawBehaviorTrackingVideoMetadata', 'Behavior'),
                           ('RawEyeTrackingVideoMetadata', 'Eye'),
                           ('RawFaceTrackingVideoMetadata', 'Face')]
    
    report = {}
    for cam, name in cam_report_keys:
        if cam in paths:
            cam_meta = read_json(paths[cam])
            cam_meta = cam_meta['RecordingReport']
            report[name] = {}
            
            lost = cam_meta['FramesLostCount']
            recorded = cam_meta['FramesRecorded']
            
            report[name]['lost frame count'] = lost
            report[name]['recorded frame count'] = recorded
            report[name]['percent lost'] = 100*lost/(lost+recorded)

    save_file = os.path.join(FIG_SAVE_DIR, prefix+'cam_frame_report.json')
    save_json(report, save_file)
    

def camera_frame_grabs(paths, syncDataset, FIG_SAVE_DIR, epoch_start_times, epoch_frame_nums = [4,2,4], prefix='', cam_video_keys=None, num_frames=10):
    
    if cam_video_keys is None:
        cam_video_keys = [('RawBehaviorTrackingVideo', 'Behavior'),
                           ('RawEyeTrackingVideo', 'Eye'),
                           ('RawFaceTrackingVideo', 'Face')]
    
    videos_present = [c[0] for c in cam_video_keys if c[0] in paths]
    num_videos = len(videos_present)
    
    #get frames spanning the 3 script epochs
    frames_to_grab = get_frames_from_epochs(videos_present, syncDataset, epoch_start_times, epoch_frame_nums)
    
    fig = plt.figure(constrained_layout=True, facecolor='0.5')
    gs = gridspec.GridSpec(num_videos, num_frames, figure=fig)
    gs.update(wspace=0.0, hspace=0.0)
    for ic, cam in enumerate(videos_present):
        video_path = paths[cam]
        v = cv2.VideoCapture(video_path)
        total_frames = v.get(cv2.CAP_PROP_FRAME_COUNT)
        
        for i,f in enumerate(frames_to_grab):
            v.set(cv2.CAP_PROP_POS_FRAMES, f)
            ret, frame = v.read()
            ax = fig.add_subplot(gs[ic, i])
            ax.imshow(frame)
            ax.axis('off')
    
    plt.tight_layout()
    
def get_frames_from_epochs(syn, epoch_start_times, epoch_frame_nums):
    
    
    

def make_metadata_json(behavior_pickle, replay_pickle, FIG_SAVE_DIR, prefix=''):
    
    report = {}
    report['mouse_id'] = replay_pickle['mouse_id']
    report['foraging_id'] = replay_pickle['foraging_id']['value']
    report['start time'] = behavior_pickle['start_time'].strftime('%Y%m%d%H%M%S')
    
    save_file = os.path.join(FIG_SAVE_DIR, prefix+'metadata.json')
    save_json(report, save_file)
    

def save_json(to_save, save_path):
    
    save_dir = os.path.dirname(save_path)
    if not os.path.exists(save_dir):
        os.mkdir(save_dir)
        
    with open(save_path, 'w') as f:
        json.dump(to_save, f, indent=2)


def save_figure(fig, save_path):
    
    save_dir = os.path.dirname(save_path)
    if not os.path.exists(save_dir):
        os.mkdir(save_dir)
    
    fig.savefig(save_path)
    

def read_json(path):
    
    with open(path, 'r') as f:
        j = json.load(f)
    
    return j

    
    
