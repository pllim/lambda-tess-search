"""This module defines all the Lambda functions to be deployed to AWS.
The function name would be AWS Lambda function name as well. Everything to be
packaged to Lambda must be contained *inside* the function. Module level
imports are for local testing or packaging use only.

Use ``bert.constants.AWS_LAMBDA_FUNCTION`` to check if function is running
locally or in AWS.

"""
from bert import binding, utils


@binding.follow('noop')
def bert_tess_fullframe_worker_2():
    """Collect light curve data from individual full frame images.
    Use the data to build a light curve file.
    Then, upload the file to S3 bucket.

    TESS FFI Light Curve Format documented at
    https://archive.stsci.edu/missions/tess/doc/EXP-TESS-ARC-ICD-TM-0014.pdf#page=32

    """
    import os
    from datetime import datetime

    import boto3
    from astropy.table import Table

    work_queue, done_queue, ologger = utils.comm_binders(
        bert_tess_fullframe_worker_2)

    s3 = boto3.resource('s3')
    inbucket = s3.Bucket(name=os.environ.get('CACHEBUCKETNAME'))
    bucket_name = os.environ.get('AWSBUCKETNAME')
    bucket = s3.Bucket(name=bucket_name)
    homedir = os.environ.get('HOME')

    # NOTE: To test this, use the example below as the manual test event.
    # DEBUG=true setting will grab it (cannot use DynamoDB but irrelevant
    # for now).
    #
    # Example event:
    # {
    #   "tic_id": "25155310",
    #   "sector": 1,
    #   "camera": 4,
    #   "ccd": 1
    #   "radius": 2.5,
    #   "cutout_width": 30,
    #   "use_cache": "true"
    # }
    for event in work_queue:
        tic_id = event['tic_id']
        sector = int(event['sector'])
        camera = int(event['camera'])
        ccd = int(event['ccd'])
        radius = float(event['radius'])
        cutout_width = int(event['cutout_width'])

        sec_id = f's{sector:04}-{camera}-{ccd}'
        in_pfx = f'tic{tic_id:0>12}/{sec_id}/r{radius}/w{cutout_width}'
        basename = f'tic{tic_id:0>12}_{sec_id}_lcc.fits'
        s3key = f'tic{tic_id:0>12}/{basename}'
        outfilename = os.path.join(homedir, basename)

        # Use cached LC generated by previous run and skip recalculations.
        # Skipping also means BLS Lambda listening for S3 upload will not run.
        use_cache = event['use_cache'] == 'true'

        # If this output exists and user wants to use the cache, there is
        # nothing to do.
        if use_cache:
            try:
                s3.Object(bucket_name, s3key).load()
            except Exception:  # Does not exist
                pass
            else:  # It exists; nothing to do
                ologger.info(f'{s3key} exists, skipping...')
                continue

        # Table header
        lc_meta = {
            'TELESCOP': 'TESS',
            'CAMERA': camera,
            'SECTOR': sector,
            'CCD': ccd,
            'OBJECT': f'TIC {tic_id}',
            'RADESYS': 'ICRS',
            'AP_RAD': radius,
            'SKYWIDTH': cutout_width,
            'DATE': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}

        # f4 = np.float32, f8 = np.float64, i4 = np.int32
        lc_tab = Table(names=('TIME', 'SAP_FLUX', 'SAP_BKG', 'QUALITY'),
                       dtype=('f8', 'f4', 'f4', 'i4'),
                       meta=lc_meta)

        # Grab all the light curve data points and piece them together.
        for obj in inbucket.objects.filter(
                Prefix=in_pfx, RequestPayer='requester'):
            filename = os.path.join(homedir, os.path.basename(obj.key))
            inbucket.download_file(
                obj.key, filename, ExtraArgs={"RequestPayer": "requester"})

            with open(filename, 'r') as fin:
                row = fin.read().split(',')

            # Clean up
            os.remove(filename)

            midtime = float(row[0])
            signal = float(row[1])
            background = float(row[2])
            dqflag = int(row[3])
            xpos = int(row[4])
            ypos = int(row[5])
            ra = float(row[6])
            dec = float(row[7])

            lc_tab.add_row((midtime, signal, background, dqflag))

        # Sort table by observation time.
        lc_tab.sort('TIME')

        # More metadata
        lc_tab.meta.update({
            'RA_OBJ': ra,
            'DEC_OBJ': dec,
            'APCEN_X': xpos,
            'APCEN_Y': ypos})

        # Write locally to FITS table.
        # Table data and metadata will go to EXT 1.
        lc_tab.write(outfilename, format='fits')
        ologger.info(f'Light Curve File[{outfilename}]')

        # Upload to S3 bucket.
        try:
            bucket.upload_file(
                outfilename, s3key, ExtraArgs={"RequestPayer": "requester"})
        except Exception as exc:
            ologger.error(str(exc))
        else:
            ologger.info(f'Uploaded {s3key} to S3')
        finally:
            # Clean up
            os.remove(outfilename)
