FROM monetdb/monetdb-r-docker

#TODO: download fully populated stats db from

COPY init-survey-db.sh /home/monetdb/init-survey-db.sh
RUN chmod a+x /home/monetdb/init-survey-db.sh

# create default survey database
RUN su -c '/home/monetdb/init-survey-db.sh' monetdb

# TODO: passwords and secrets
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
