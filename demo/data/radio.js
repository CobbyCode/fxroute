// Snapshot of radio/stations.py DEFAULT_STATIONS and STATION_CATALOG.
// Keep provider URLs and artwork aligned with the real FXRoute catalog.
(function () {
    'use strict';

    const savedStations = [
        { id: 'groovesalad', name: 'Groove Salad', input_url: 'https://somafm.com/groovesalad130.pls', stream_url: 'https://ice4.somafm.com/groovesalad-256-mp3', image_url: '/static/station-art/groovesalad.png' },
        { id: 'suburbsofgoa', name: 'Suburbs of Goa', input_url: 'https://somafm.com/suburbsofgoa130.pls', stream_url: 'https://ice4.somafm.com/suburbsofgoa-128-aac', image_url: '/static/station-art/suburbsofgoa.png' },
        { id: 'thetrip', name: 'The Trip', input_url: 'https://somafm.com/thetrip130.pls', stream_url: 'https://ice4.somafm.com/thetrip-128-aac', image_url: '/static/station-art/thetrip.jpg' },
        { id: 'poptron', name: 'PopTron', input_url: 'https://somafm.com/poptron130.pls', stream_url: 'https://ice4.somafm.com/poptron-128-aac', image_url: '/static/station-art/poptron.png' },
        { id: 'dubstep', name: 'Dub Step Beyond', input_url: 'https://somafm.com/dubstep256.pls', stream_url: 'https://ice4.somafm.com/dubstep-256-mp3', image_url: '/static/station-art/dubstep.png' },
        { id: 'live', name: 'SomaFM Live', input_url: 'https://somafm.com/live130.pls', stream_url: 'https://ice4.somafm.com/live-128-aac', image_url: '/static/station-art/live.jpg' },
        { id: 'gsclassic', name: 'Groove Salad Classic', input_url: 'https://somafm.com/gsclassic130.pls', stream_url: 'https://ice4.somafm.com/gsclassic-128-aac', image_url: '/static/station-art/gsclassic.jpg' },
    ];

    const somaNames = {
        '7soul': 'Seven Inch Soul',
        beatblender: 'Beat Blender',
        bootliquor: 'Boot Liquor',
        brfm: 'Black Rock FM',
        cliqhop: 'cliqhop idm',
        covers: 'Covers',
        deepspaceone: 'Deep Space One',
        digitalis: 'Digitalis',
        doomed: 'Doomed',
        dronezone: 'Drone Zone',
        dz2: 'Drone Zone 2',
        dubstep: 'Dub Step Beyond',
        fluid: 'Fluid',
        folkfwd: 'Folk Forward',
        groovesalad: 'Groove Salad',
        groovesalad2: 'Groove Salad 2',
        gsclassic: 'Groove Salad Classic',
        illstreet: 'Illinois Street Lounge',
        indiepop: 'Indie Pop Rocks!',
        lush: 'Lush',
        missioncontrol: 'Mission Control',
        poptron: 'PopTron',
        secretagent: 'Secret Agent',
        seventies: 'Left Coast 70s',
        sonicuniverse: 'Sonic Universe',
        spacestation: 'Space Station Soma',
        suburbsofgoa: 'Suburbs of Goa',
        thetrip: 'The Trip',
        thistle: 'ThistleRadio',
        u80s: 'Underground 80s',
        metal: 'Metal Detector',
        reggae: 'Heavyweight Reggae',
        vaporwaves: 'Vaporwaves',
        synphaera: 'Synphaera Radio',
        darkzone: 'The Dark Zone',
        tikitime: 'Tiki Time',
        bossa: 'Bossa Beyond',
        insound: 'The In-Sound',
    };

    const somaImageUrls = {
        brfm: 'https://api.somafm.com/logos/512/brfm512.jpg',
        fluid: 'https://api.somafm.com/logos/512/fluid512.jpg',
        gsclassic: 'https://api.somafm.com/logos/512/gsclassic512.jpg',
        missioncontrol: 'https://api.somafm.com/logos/512/missioncontrol512.jpg',
        seventies: 'https://api.somafm.com/logos/512/seventies512.jpg',
        thetrip: 'https://api.somafm.com/logos/512/thetrip512.jpg',
        thistle: 'https://api.somafm.com/logos/512/thistle512.jpg',
        synphaera: 'https://api.somafm.com/logos/512/synphaera512.jpg',
        darkzone: 'https://api.somafm.com/logos/512/darkzone512.jpg',
        tikitime: 'https://api.somafm.com/logos/512/tikitime512.jpg',
        bossa: 'https://api.somafm.com/logos/512/bossa-512.jpg',
        insound: 'https://api.somafm.com/logos/512/insound-512.jpg',
    };

    const savedById = new Map(savedStations.map((station) => [station.id, station]));
    const somaStations = Object.entries(somaNames).map(([id, name]) => {
        const saved = savedById.get(id);
        return {
            id,
            name,
            input_url: saved?.input_url || `https://api.somafm.com/${id}130.pls`,
            stream_url: saved?.stream_url || `https://ice5.somafm.com/${id}-128-aac`,
            image_url: somaImageUrls[id] || `https://api.somafm.com/logos/512/${id}512.png`,
            provider: 'SomaFM',
        };
    });

    const fipStations = [
        ['fip-main', 'FIP', 'fip'],
        ['fip-rock', 'FIP Rock', 'fiprock'],
        ['fip-jazz', 'FIP Jazz', 'fipjazz'],
        ['fip-groove', 'FIP Groove', 'fipgroove'],
        ['fip-world', 'FIP Monde', 'fipworld'],
        ['fip-nouveautes', 'FIP Nouveaut\u00e9s', 'fipnouveautes'],
        ['fip-reggae', 'FIP Reggae', 'fipreggae'],
        ['fip-electro', 'FIP Electro', 'fipelectro'],
        ['fip-metal', 'FIP Metal', 'fipmetal'],
        ['fip-pop', 'FIP Pop', 'fippop'],
        ['fip-hiphop', 'FIP Hip-Hop', 'fiphiphop'],
    ].map(([id, name, slug]) => ({
        id,
        name,
        input_url: `https://icecast.radiofrance.fr/${slug}-midfi.mp3?id=openapi`,
        stream_url: `https://icecast.radiofrance.fr/${slug}-midfi.mp3?id=openapi`,
        image_url: 'https://www.radiofrance.fr/pikapi/images/a8903fd7-01e2-45a1-b768-61e3d8e1ff6a/512x512',
        provider: 'FIP',
    }));

    const bbcStations = [
        ['bbc-radio-1', 'BBC Radio 1', 'https://stream.live.vc.bbcmedia.co.uk/bbc_radio_one', '/static/station-art/bbc-radio-1.svg'],
        ['bbc-radio-2', 'BBC Radio 2', 'https://stream.live.vc.bbcmedia.co.uk/bbc_radio_two', '/static/station-art/bbc-radio-2.svg'],
        ['bbc-radio-4', 'BBC Radio 4', 'https://stream.live.vc.bbcmedia.co.uk/bbc_radio_fourfm', '/static/station-art/bbc-radio-4.svg'],
    ].map(([id, name, stream_url, image_url]) => ({ id, name, input_url: stream_url, stream_url, image_url, provider: 'BBC' }));

    const otherStations = [
        ['kexp-main', 'KEXP Main', 'https://kexp.streamguys1.com/kexp160.aac', 'https://www.kexp.org/static/assets/img/logo-header.svg'],
        ['wfmu-main', 'WFMU Main', 'http://stream0.wfmu.org/freeform-128k.mp3', 'https://wfmu.org/images/wfmu-logo.svg'],
        ['radio-calico', 'Radio Calico', 'https://stream.radio-calico.com/calico.mp3', 'https://www.radio-calico.com/wp-content/uploads/2023/03/RadioCalicoLogo-green-300px.png'],
        ['jb-radio-2', 'JB Radio-2', 'https://mediacp.jb-radio.net:8001/aac', 'https://jb-radio.net/sites/all/themes/radio4b/favicon.ico'],
        ['radio-swiss-jazz', 'Radio Swiss Jazz', 'https://stream.srg-ssr.ch/srgssr/rsj/aac/96', 'https://www.radioswissjazz.ch/social-media/rsj-web.png'],
        ['radio-swiss-pop', 'Radio Swiss Pop', 'https://stream.srg-ssr.ch/srgssr/rsp/aac/96', 'https://www.radioswisspop.ch/social-media/rsp-web.png'],
        ['radio-swiss-classic', 'Radio Swiss Classic', 'https://stream.srg-ssr.ch/srgssr/rsc_de/aac/96', 'https://www.radioswissclassic.ch/social-media/rsc-web.png'],
        ['kcrw-eclectic24', 'KCRW Eclectic24', 'https://streams.kcrw.com/e24_aac', 'https://pressroom.kcrw.com/wp-content/uploads/sites/7/2012/05/KCRW_LOGO-Hero400.jpg'],
    ].map(([id, name, stream_url, image_url]) => ({ id, name, input_url: stream_url, stream_url, image_url, provider: 'Other Stations' }));

    const radioParadise = [
        ['rp-main', 'Radio Paradise Main Mix', 'https://stream.radioparadise.com/aac-320', 'https://img.radioparadise.com/channels/0/0/cover_512x512/0.jpg'],
        ['rp-mellow', 'Radio Paradise Mellow Mix', 'https://stream.radioparadise.com/mellow-320', 'https://img.radioparadise.com/channels/0/1/cover_512x512/0.jpg'],
        ['rp-rock', 'Radio Paradise Rock Mix', 'https://stream.radioparadise.com/rock-320', 'https://img.radioparadise.com/channels/0/2/cover_512x512/0.jpg'],
        ['rp-global', 'Radio Paradise Global Mix', 'https://stream.radioparadise.com/global-320', 'https://img.radioparadise.com/channels/0/3/cover_512x512/0.jpg'],
    ].map(([id, name, stream_url, image_url]) => ({ id, name, input_url: stream_url, stream_url, image_url, provider: 'Radio Paradise' }));

    const catalogStations = [...radioParadise, ...somaStations, ...fipStations, ...bbcStations, ...otherStations];

    window.FXROUTE_DEMO_RADIO = {
        savedStations,
        catalogStations,
    };
})();
